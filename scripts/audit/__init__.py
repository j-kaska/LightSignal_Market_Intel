"""
LightSignal — audit
===================
A read-only harness that independently checks the article pipeline's work.

Nothing in this package writes to news_feed.csv or dc_consolidated.csv. Every
run asserts those files are byte-identical at exit (see io_utils.read_only_guard).
"""
