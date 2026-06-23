"""Unit tests for SlackDaemon._classify_interrupt (soft vs hard interrupt)."""

from slack_daemon import SlackDaemon

c = SlackDaemon._classify_interrupt


class TestClassifyInterrupt:
    def test_bang_prefix_is_hard_with_remainder(self):
        assert c("! 改成只回答人民币") == ("hard", "改成只回答人民币")

    def test_bang_alone_is_hard_stop(self):
        assert c("!") == ("hard", "")

    def test_english_stopword_alone(self):
        assert c("stop") == ("hard", "")
        assert c("STOP") == ("hard", "")

    def test_chinese_stopword_alone(self):
        assert c("停") == ("hard", "")
        assert c("中断") == ("hard", "")

    def test_chinese_stop_prefix_keeps_instruction(self):
        assert c("停 只回答人民币报价") == ("hard", "只回答人民币报价")

    def test_english_stop_prefix_keeps_instruction(self):
        kind, rem = c("stop do it differently")
        assert kind == "hard" and rem == "do it differently"

    def test_normal_message_is_soft(self):
        msg = "问中文价格时只需要回答人民币报价，不要把海外的价格也吐出来"
        assert c(msg) == ("soft", msg)

    def test_soft_trims_whitespace(self):
        assert c("   keep going please   ") == ("soft", "keep going please")

    def test_bang_midword_not_triggered(self):
        # only a *leading* '!' is an interrupt; '!' elsewhere is normal text
        assert c("run tests!") == ("soft", "run tests!")
