"""The retrieval evaluation harness (RET-01).

Measures whether search surfaces the right evidence for a query, against a
synthetic corpus with hand-labelled gold answers. It exists so that RET-02 and
RET-03 can state a number instead of an impression, and so that a change making
retrieval *worse* fails a test rather than being noticed months later.

See ``docs/retrieval-harness.md`` for what the numbers mean and, more
importantly, what they do not.
"""
