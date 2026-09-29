"""Stand-ins and canned values that two or more test modules share.

Admission rule: a double lives here only when every consumer scripts it the
same way. Doubles that merely share a name stay in their own test module.
The one exception is ``FakeSignedClient``'s ``cancel_removes_order``: the
startup suite sets it and the kill-switch suite does not.
"""
