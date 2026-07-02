# Phase 2 — 四個 PR 的啟動 prompts

每段 prompt 自包含，可直接貼進新的 Claude Code session。
依序執行：PR 1 → PR 2 → PR 3 → PR 4，後面的 PR 假設前面的已合進整合分支 `hyperliquid-adapter`。
每個 PR 完成後用 `/commit-ready` 收尾、`/open-pr` 對 `hyperliquid-adapter` 開 PR。

使用方式：先 checkout 到要當 base 的分支（base 上必須有 phase2 規格文件），再開新 session
貼上對應 prompt——prompt 會自己從當下分支開新分支並在新分支上實作，不會動到 base。

---

## PR 1 — 決策契約遷移 ＋ RiskGate

```
實作 Hyperliquid Phase 2 的 PR 1：決策契約遷移 ＋ RiskGate。

分支（先做這步再動任何檔案）：
- 確認 working tree 乾淨（git status），且目前分支上存在
  contrib/hyperliquid_perp/docs/phase2-spec.md。任一不符 → 停下來向使用者確認 base，不要繼續。
- 從目前分支開新分支 feat/phase2-pr1-decision-contract 並切換過去，所有實作都在新分支上進行。

先讀規格（這是唯一正確來源，實作不得偏離）：
- contrib/hyperliquid_perp/docs/DESIGN.md 的「Part 2 — Decision schema & order flow」（structured target contract）
- contrib/hyperliquid_perp/docs/phase2-spec.md §2（Risk & Margin，含 2.3 clamp、2.4 step/deadband/min_confidence）
- 現有程式：contrib/hyperliquid_perp/integration/decision_adapter.py、integration/trading_graph.py、main.py

範圍：
1. 新增 domains/perp/target_decision.py：TargetDecision（decision_mode / target_side /
   requested_target_margin_pct / confidence / rationale / key_risks）＋ 解析與 cross-field 驗證。
   所有 invalid 組合（long/short+0、flat+nonzero、maintain_current+target、margin 出界或非整數步進、
   confidence 非數字或出 0–1）一律 fail-closed 成 maintain_current ＋ risk_action=invalid_fail_closed，
   不做 silent rounding、不從舊 rating 或前一輪 target 推測遺失欄位，並保留原始 response。
2. Prompt 改版：不修改上游 tradingagents package。在 trading_graph.build_graph 注入的 context
   尾端加輸出格式指示，要求引擎在 final_trade_decision 輸出 structured JSON；在我們這側
   抽出 JSON block 解析。解析失敗 → invalid_output、fail-closed 成 maintain_current。
3. 新增 domains/perp/risk_gate.py（純函式、無 I/O）：
   - step / 範圍 / 型別驗證（整數 0–100）
   - min_confidence=0.3 門檻（只擋 set_target，confidence 不參與 sizing）
   - clamp 到 max_target_margin_pct=60，記 risk_action=clamped（保留 requested 與 approved 兩個值）
   - deadband：target_side 與現有持倉同向且 |approved - current_margin_pct| < 1 → 不建單、
     no_order_reason=within_deadband；flip 與 flat 平倉不適用 deadband
   - effective_leverage 與 available margin 獨立檢查（不能只靠 margin allocation 上限）
   - 輸出對齊 phase2-data.md §7 ai_outputs 欄位：approved_target_margin_pct / risk_action /
     risk_reason / no_order_reason / target_margin / target_notional / target_signed_notional /
     delta_notional
4. 退役 Phase 1 rating 管線：decision_adapter.py 的 resolve_rating / rating_to_target / rebalance /
   tier 設定（target_size_pct、confidence tiers）。Phase 1 audit log 格式保留但 Phase 2 不讀取。
   main.py 的 run_engine 改走新契約。
5. config.py / configs/hyperliquid.example.yaml 新增 risk: 與 decision: 區塊
   （phase2-spec.md §2 開頭的預設值）。

測試（pytest，沿用 contrib/hyperliquid_perp/tests/ 的既有風格）：
- 四種合法組合（set_target+long/short+1–100、set_target+flat+0、maintain_current+null+null）
- 每一種 invalid 組合各一個 case，驗證 fail-closed 輸出形狀
- clamp：requested 61–100 → approved 60、risk_action=clamped
- deadband：同向 <1% 不建單；flip 與 flat 即使差距極小也必須執行
- low confidence（set_target + confidence<0.3）→ invalid_fail_closed、risk_reason=low_confidence
- JSON 解析失敗 / 缺欄位 / 多餘欄位的 fail-closed 路徑

注意事項：
- 全程用 Decimal（沿用 Phase 1 慣例），不用 float 做金額運算。
- 既有記憶提醒：exposure 用 committed margin 不是 gross notional；sizing/估值用 mark、
  fill 模擬用 mid；HyperliquidTradingGraph 巢狀 class 是刻意的 lazy import，不要動。

完成後：跑全部 pytest、/commit-ready、/open-pr 對 hyperliquid-adapter。
```

---

## PR 2 — SQLite persistence ＋ 帳務 / 清算模型

```
實作 Hyperliquid Phase 2 的 PR 2：SQLite persistence ＋ paper 帳務與清算模型。
前置：PR 1（決策契約 ＋ RiskGate）已合入。

分支（先做這步再動任何檔案）：
- 確認 working tree 乾淨（git status），且目前分支上存在
  contrib/hyperliquid_perp/docs/phase2-data.md 與 PR 1 的 domains/perp/risk_gate.py。
  任一不符 → 停下來向使用者確認 base，不要繼續。
- 從目前分支開新分支 feat/phase2-pr2-persistence-accounting 並切換過去，所有實作都在新分支上進行。

先讀規格（唯一正確來源）：
- contrib/hyperliquid_perp/docs/phase2-data.md 全文（SQLite source of truth、tables、
  transaction 語意、去重鍵、CSV 對映）
- contrib/hyperliquid_perp/docs/phase2-execution.md §6（帳務公式、fee/funding、
  6.6.1 estimated liquidation price、6.6.2 model validation）
- 現有 fixtures：contrib/hyperliquid_perp/tests/fixtures/（clearinghouse_state.json、
  meta_and_asset_ctxs.json 可用於清算模型比對）

範圍：
1. 新增 persistence/ package：
   - db.py：connection 管理、schema_migrations table、transaction context manager
   - schema：8 張 export logical tables（ai_inputs、decision_attempts、ai_outputs、orders、
     fills、funding_events、account_snapshots、position_snapshots，欄位完全依 phase2-data.md
     §5–§12）＋ 6 張 internal tables（runs、scheduler_state、execution_plans、
     current_positions、current_account_state、schema_migrations）
   - unique constraints：slice_id（run_id+plan_id+flip_leg+slice_index）、
     (run_id, symbol, funding_timestamp)、decision_attempt_id
   - repository 層：typed insert/update；current_positions / current_account_state 必須和
     造成變化的 fill/fee/funding event 在同一個 transaction 內更新
2. 新增 paper/accounting.py：
   - fill 過帳：fee = abs(qty*price)*taker_fee_rate、realized PnL（long/short 公式）、
     平均 entry（加倉更新、減倉不變）、wallet_balance 扣 fee — 全部在一個 transaction
   - funding exactly-once：funding_pnl = -signed_position_notional * funding_rate，
     pending→posted 只過帳一次；rate 取不到記 funding_pending，之後由 funding history 補帳，
     不得用 0 或舊 rate 偽造
   - 帳戶公式（execution §6.1）：account_equity、available_balance、effective_leverage、
     margin_ratio、used_initial_margin、total_maintenance_margin
   - accounting replay：從 committed fills/fees/funding 重建 positions/account state，
     與 materialized current_* 表比對，回報 mismatch counters
3. 清算模型（execution §6.6.1）：
   - margin tier 從 Hyperliquid meta 的 margin table 取得，不 hardcode
   - candidate-price 函數 f(p) = account_equity(p) - total_maintenance_margin(p)，
     tier 依 candidate price 下的 notional 重新選擇
   - long 向 0 搜、short 向上 bracket，deterministic bisection 求根
   - tick rounding：long round up、short round down
   - 無正數解 → null；f(mark)<=0 → 已可清算狀態（回報給呼叫端，不建一般 SL）
   - snapshot 記錄 margin_tier_id / maintenance_margin_rate / maintenance_deduction /
     liquidation_model_version

測試：
- transaction：COMMIT 前 crash 模擬 → 全部回滾；COMMIT 後重啟 → 不重複套用
- unique constraint：同一 slice_id / funding key 重複寫入被擋
- 帳務公式各一組數字案例（含 §6.2 的 20%/5x 範例）
- funding exactly-once：retry / restart 不重複過帳；pending 補帳只套用一次
- 清算模型（§6.6.2 全部案例）：long、short、無正數清算價、當前已可清算、跨 margin tier、
  fee/funding 變動後重算、多倉位 cross margin、tick rounding；
  用 recorded clearinghouseState fixtures 與 liquidationPx 比對並記錄容許誤差
- accounting replay：同一組 events 重建結果 deterministic 且與 current_* 一致

注意事項：
- 全程 Decimal；SQLite 存字串或整數最小單位，避免 REAL 浮點污染。
- CSV 匯出不在本 PR（PR 4），但欄位 shape 要先對齊 phase2-data.md。
- 既有記憶提醒：sizing/估值用 mark price、fill 模擬用 mid price。

完成後：跑全部 pytest、/commit-ready、/open-pr 對 hyperliquid-adapter。
```

---

## PR 3 — 執行引擎（TWAP / flip、SL / TP、market monitor）

```
實作 Hyperliquid Phase 2 的 PR 3：paper 執行引擎。
前置：PR 1（契約+RiskGate）與 PR 2（persistence+帳務）已合入。

分支（先做這步再動任何檔案）：
- 確認 working tree 乾淨（git status），且目前分支上存在
  contrib/hyperliquid_perp/docs/phase2-execution.md 與 PR 2 的 persistence/ package。
  任一不符 → 停下來向使用者確認 base，不要繼續。
- 從目前分支開新分支 feat/phase2-pr3-execution-engine 並切換過去，所有實作都在新分支上進行。

先讀規格（唯一正確來源）：
- contrib/hyperliquid_perp/docs/phase2-execution.md §1–§5 全文
  （TWAP/flip、SL/TP、paper_market 成交模型、§5.3 事件順序、§5.5 market monitor）
- contrib/hyperliquid_perp/ports.py（ExchangeMarketData port — snapshot 來源）
- PR 2 的 persistence/ 與 paper/accounting.py 介面

範圍：
1. 時間與資料抽象（先定介面再寫邏輯）：注入式 Clock 與 snapshot provider，
   讓引擎在測試中可快轉、不依賴真實 sleep；正式執行時綁 wall clock ＋ ExchangeMarketData。
2. TWAP / flip plan（§1.1–1.3）：
   - 切分：min_order_qty = ceil_to_step(min_notional/mid)、planned_slices = min(max_legal, 120)、
     0 slices → reject/residual、1 slice → paper_market、2–120 → TWAP 每 30 秒一 slice
   - 整數步進分配（total_steps / base_steps / extra_steps），總和必須正好等於 total_qty
   - rounding_residual_qty 記錄、不向上湊整
   - flip：close leg（reduce-only）完全成交且確認 flat 後，重跑 deterministic RiskGate
     （不重呼 AI）才開 open leg；兩 legs 共用 output_id / flip_plan_id、合計 ≤120 slices；
     失敗記 flip_incomplete，不開反向倉
   - plan 最晚建立後一小時進 terminal state；deadline 未成交數量記 residual_qty
3. 成交模擬（§5.1–5.2）：
   - paper_market：active_from 後第一個有效 snapshot 以完整數量成交，不模擬部分成交
   - fill_price = mid ± slippage_bps/10000；SL/TP trigger 用 mark、成交參考用 mid
   - 驗證不過 → status=rejected ＋ status_reason，不進成交流程
   - 無 mid → pending_market_data，不得用 mark 或舊價偽造 fill
4. Market data 新鮮度（§1.1）：每 slice 發新 request、5 秒 timeout、同一 snapshot 需同時有
   mid 與 mark 才有效；記 requested_at / received_at / latency；連續 3 slices 失敗 →
   paused_market_data，期間仍每 30 秒探測；錯過的 slices 不補跑不爆量；恢復後 mark 已越過
   SL trigger → 立即以當下 mid+adverse slippage 模擬 gap_stop_fill
5. SL / TP（§2–§4）：
   - invariants：position=0 → 無 active SL/TP；position≠0 → SL 覆蓋全部倉位
   - SL：每次 position-changing fill 後重算（§3.2 五步）；公式 §3.7；range 檢查 §3.3/3.4；
     liq buffer 不足 → 不掛 SL 直接平倉（§3.6）；estimated_liquidation_price=null 時
     只用 entry-based range、不套 liq buffer
   - TP：TWAP 啟動前取消 TP 保留 SL；執行期間不建 TP；plan terminal 後以最終 position 建 TP
     （tp_threshold=20%）；flip 中 close leg 持續更新 SL、平倉完成取消、open leg 首筆 fill 後
     立即建 SL、整個 plan 結束才建 TP
6. §5.3 固定事件順序（同一 snapshot）：更新價格 → 過帳到期 funding → liquidation/emergency →
   SL → TP → TWAP slice/paper_market → 更新 position/PnL/fees/margin → SL/TP lifecycle →
   account snapshot。風險出場平倉後立即取消同 plan 剩餘 slices 並 terminal；部分減倉也
   終止原 plan，剩餘 target 留給下一個 4h decision。
7. Market monitor（§5.5）：有 position 或 active plan/order/SL/TP → 每 30 秒 poll；
   全空 → 停止輪詢。monitor 與 slice 同一 tick 共用同一份 snapshot，不重複呼叫 API。
8. 所有 order / fill / plan 狀態變化都經 PR 2 的 persistence 層，同一成交的變更在
   同一個 SQLite transaction 內。

測試（全部用注入 clock ＋ fake snapshot provider，不真 sleep）：
- 切分數學：total_qty=1.03、step=0.01、4 slices → 0.26/0.26/0.26/0.25；0/1/2–120 slice 分支
- 事件順序 determinism：同一 snapshot 同時觸發 funding+SL+slice 的處理順序
- SL 觸發後同 snapshot 不再執行會重建倉位的 slice
- flip：close 未完成不開 open；RiskGate 拒 open leg → flip_incomplete
- 新鮮度：timeout slice 留空、連 3 失敗 pause、恢復不補跑、gap_stop_fill
- SL/TP invariants 與 lifecycle 各狀態轉換
- pending_market_data 與 rejected 的區分

注意事項：
- 全程 Decimal。
- 既有記憶提醒：fill 模擬用 mid、sizing/估值用 mark；short SL 檢查 level <= mark 是對的
  （之前 review 誤報過），candle 過濾 <= end 也是對的。

完成後：跑全部 pytest、/commit-ready、/open-pr 對 hyperliquid-adapter。
```

---

## PR 4 — Scheduler ＋ CLI ＋ CSV export ＋ 驗收器

```
實作 Hyperliquid Phase 2 的 PR 4：scheduler、CLI、CSV export 與驗收器。
前置：PR 1–3 已合入。

分支（先做這步再動任何檔案）：
- 確認 working tree 乾淨（git status），且目前分支上存在 PR 3 的 paper 執行引擎模組
  （paper/ 下的 execution 相關檔案）。任一不符 → 停下來向使用者確認 base，不要繼續。
- 從目前分支開新分支 feat/phase2-pr4-scheduler-export 並切換過去，所有實作都在新分支上進行。

先讀規格（唯一正確來源）：
- contrib/hyperliquid_perp/docs/phase2-spec.md §3（rolling 4h cycle、3.1 retry）、
  §5（驗收標準與 summary 指標）
- contrib/hyperliquid_perp/docs/phase2-execution.md §1.2（重啟 reconciliation 九步）
- contrib/hyperliquid_perp/docs/phase2-data.md §1.1（CSV export 時機與 atomicity）、
  §6（decision_attempts 欄位）
- 現有 contrib/hyperliquid_perp/main.py（Phase 1 CLI，需保留 --context-only）

範圍：
1. paper/scheduler.py：
   - rolling 4h：新 run 立即執行；next_decision_at = 實際 decision_at + 4h；
     重啟時 now < next → 等待、now >= next → 立即執行一次、不補跑錯過的 intervals
   - decision_attempt_id = deterministic(run_id + scheduled_at)；retryable 失敗最多 3 次
     （間隔 10s / 30s），三次皆敗 → decision_status=api_failed、不建 target/order、
     不沿用上次 output、下一 cycle = scheduled_at + 4h
   - AI 回應但 schema/cross-field 無效 → invalid_output、maintain_current、不重呼 AI、
     cycle 視為完成、下一次 = 實際 decision_at + 4h
   - retry state 先寫 SQLite；重啟只能延續未滿 3 次的同一 attempt，不歸零、不重複 decision
   - 重啟 reconciliation（execution §1.2 九步）：發現 unfinished plan → 取消標 canceled_restart、
     residual_qty、由 committed fills 重建 position、補帳 funding、先處理 gap SL/TP、
     reconcile 後立即開新 cycle
2. CLI 改 subcommand（保留 Phase 1 行為）：
   - python -m contrib.hyperliquid_perp paper --coin BTC （長駐 paper run）
   - python -m contrib.hyperliquid_perp export --run-id <id> --output-dir <dir>
   - python -m contrib.hyperliquid_perp validate --run-id <id> （驗收器）
   - 既有 --context-only 路徑不變
3. CSV export（phase2-data §1.1–1.2）：
   - 每 cycle 完成 reconciliation 後、正常 shutdown、手動 command 三個時機
   - per-run-id 全量匯出、8 個 CSV 欄位完全依 phase2-data.md §5–§12
   - .tmp 寫入 → flush → atomic replace；export 失敗只記 export_failed，
     不回滾交易 state、不停 monitor
4. 驗收器（spec §5）：
   - 13 項 summary 指標（cycle_count、order_count、fill_count、rejected/orphan counts、
     snapshot_mismatch_count、accounting_replay_mismatch_count、max_exposure_pct、
     max_effective_leverage、total_pnl、total_fees、net_funding_pnl）
   - 檢查鏈路完整性：order 有 output_id、fill 有合法 order_id、position/account snapshot
     可重算、replay 一致
   - 輸出「可進 Phase 3」判定（cycle>=30、orphan=0、mismatch=0）

測試：
- next_decision_at 各情境（首跑、延遲恢復 16:00 → 下次 20:00、新 run_id 立即執行）
- retry：attempt counter 跨重啟持續、三次失敗 api_failed、invalid_output 不重呼 AI
- 重啟 reconciliation：unfinished plan → canceled_restart → 立即新 cycle
- export atomicity：中斷不留半寫 CSV；export 失敗不影響 DB state
- 驗收器對一組合成 run 資料算出正確指標與 orphan/mismatch

完成後：跑全部 pytest、/commit-ready、/open-pr 對 hyperliquid-adapter。
合入後啟動 BTC 4h × 30 cycles 驗收 run（約 5 天），用 validate 檢查進 Phase 3 條件。
```
