import unittest

from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillValidationError,
    repair_competitor_breakdown_comment_semantics,
    validate_competitor_breakdown_question_expansion_output,
)


class CommentValidation3ATest(unittest.TestCase):
    def _input(self, transcript: str = "原文没有提供其他评论事实。") -> dict:
        return {
            "source_id": "comment_validation_fixture",
            "transcript": transcript,
            "comments": [{"text": "原唱归属争议"}],
            "domain_context": {
                "description": "音乐、作品和音乐人物的音乐经历",
                "question_expansion_policy": {
                    "primary_content_carrier_required": True,
                    "primary_carrier_signal_terms": ["音乐", "歌曲", "作品", "演唱"],
                },
            },
        }

    def _payload(self, analysis_text: str, *, signals: list[dict] | None = None) -> dict:
        if not analysis_text.lstrip().startswith("WHAT"):
            analysis_text = (
                "WHAT\n核心对象和命题以本次材料为准。\n"
                "HOW\n只记录材料中实际出现的推进及其作用。\n"
                "SO WHAT\n无有效复用参考；无法判断的内容保持 unknown。\n"
                + analysis_text
            )
        payload = {
            "source_id": "comment_validation_fixture",
            "source_content_type": "人物故事",
            "analysis_text": analysis_text,
            "schema_version": "competitor_breakdown.output.raw.v4",
            "question_expansions": [],
        }
        if signals is not None:
            payload["schema_version"] = "competitor_breakdown.output.raw.v5"
            payload["expansion_signals"] = signals
            payload["typed_expansion_leads"] = []
        return payload

    def _validates(self, analysis_text: str, *, transcript: str = "原文没有提供其他评论事实。", signals=None) -> bool:
        try:
            validate_competitor_breakdown_question_expansion_output(
                self._input(transcript),
                self._payload(analysis_text, signals=signals),
            )
        except FormalSkillValidationError:
            return False
        return True

    def test_comment_observation_is_allowed(self):
        self.assertTrue(self._validates("五、评论信号\n评论中多人表达怀旧情绪。\n六、候选复用原则与边界\n无明显短板。"))

    def test_comment_question_is_allowed(self):
        self.assertTrue(self._validates("五、评论信号\n评论中有人追问某歌手为什么没有被提到。\n六、候选复用原则与边界\n无明显短板。"))

    def test_comment_fact_is_preserved_as_an_unresolved_observation(self):
        self.assertTrue(self._validates(
            "五、评论信号\n评论中有人称某歌手此前可能已有说唱创作经历，待核实。\n六、候选复用原则与边界\n无明显短板。",
        ))

    def test_comment_causal_effect_is_rejected(self):
        self.assertFalse(self._validates("五、评论信号\n评论中很多人喜欢，因此证明这种结构提高了传播。\n六、候选复用原则与边界\n无明显短板。"))

    def test_source_context_carries_across_a_comment_observation_sentence(self):
        self.assertFalse(self._validates("五、评论信号\n评论观察：评论者喜欢两首歌。这表明该事件具有传播潜力。\n六、候选复用原则与边界\n无明显短板。"))

    def test_comment_signal_label_does_not_authorize_effect_inference(self):
        self.assertFalse(self._validates("五、评论信号\n模型结构分析：评论信号显示内容引发了观众参与，验证了选题有效性。\n六、候选复用原则与边界\n无明显短板。"))

    def test_comment_reaction_cannot_be_upgraded_to_content_effect(self):
        analysis = (
            "五、评论信号\n"
            "评论观察：多条评论表达了怀旧和共鸣，说明内容成功唤起了粉丝群体的情感共鸣。\n"
            "六、候选复用原则与边界\n"
            "无明显短板。"
        )
        self.assertFalse(self._validates(analysis))
        repaired = repair_competitor_breakdown_comment_semantics(self._input(), self._payload(analysis))
        self.assertIn("评论观察：多条评论表达了怀旧和共鸣。", repaired["analysis_text"])
        self.assertNotIn("唤起了粉丝群体", repaired["analysis_text"])
        self.assertTrue(self._validates(repaired["analysis_text"]))

    def test_local_repair_keeps_observation_and_removes_only_effect_claim(self):
        payload = self._payload("五、评论信号\n评论观察：评论者喜欢两首歌。这表明该事件具有传播潜力。\n六、候选复用原则与边界\n无明显短板。")
        repaired = repair_competitor_breakdown_comment_semantics(self._input(), payload)
        self.assertIn("评论观察：评论者喜欢两首歌。", repaired["analysis_text"])
        self.assertNotIn("传播潜力", repaired["analysis_text"])
        self.assertNotIn("这可能。", repaired["analysis_text"])
        self.assertTrue(self._validates(repaired["analysis_text"]))

    def test_local_repair_marks_only_the_comment_fact_as_pending(self):
        payload = self._payload("五、评论信号\n评论中有人补充某歌手此前已经有说唱创作经历。\n六、候选复用原则与边界\n无明显短板。")
        repaired = repair_competitor_breakdown_comment_semantics(self._input(), payload)
        self.assertIn("待核实", repaired["analysis_text"])
        self.assertIn("某歌手此前已经有说唱创作经历", repaired["analysis_text"])
        self.assertTrue(self._validates(repaired["analysis_text"]))

    def test_local_repair_applies_to_signal_text_without_dropping_the_signal(self):
        signals = [{
            "signal_id": "S1",
            "signal_kind": "comment_question",
            "signal_text": "评论中大量提及历届歌曲，这反映出观众存在稳定的经典参照系。",
            "source_anchor": "comment",
            "reason": "这些评论表明观众普遍喜欢这类内容。",
        }]
        payload = self._payload(
            "五、评论信号\n评论中有人提到历届歌曲。\n六、候选复用原则与边界\n无明显短板。",
            signals=signals,
        )
        repaired = repair_competitor_breakdown_comment_semantics(self._input(), payload)
        repaired_signal = repaired["expansion_signals"][0]
        self.assertIn("评论中大量提及历届歌曲。", repaired_signal["signal_text"])
        self.assertNotIn("反映出观众", repaired_signal["signal_text"])
        self.assertNotIn("这些评论表明", repaired_signal["reason"])
        self.assertNotIn("观众普遍喜欢", repaired_signal["reason"])

    def test_repair_does_not_truncate_author_text_before_comment_text(self):
        payload = self._payload(
            "五、评论信号\n作者观点：原文作者将事件成功归因于热爱。评论中有人表达认同，这表明内容传播成功。\n六、候选复用原则与边界\n无明显短板。"
        )
        repaired = repair_competitor_breakdown_comment_semantics(self._input(), payload)
        self.assertIn("作者观点：原文作者将事件成功归因于热爱。", repaired["analysis_text"])
        self.assertIn("评论中有人表达认同。", repaired["analysis_text"])
        self.assertNotIn("传播成功", repaired["analysis_text"])

    def test_author_viewpoint_must_keep_author_source(self):
        self.assertFalse(self._validates(
            "一、内容类型\n这首歌是最好的演唱。\n五、评论信号\n没有发现有效评论信号。\n六、候选复用原则与边界\n无明显短板。",
            transcript="作者说：这首歌是最好的演唱。",
        ))
        self.assertTrue(self._validates(
            "一、内容类型\n原文作者说这首歌是最好的演唱。\n五、评论信号\n没有发现有效评论信号。\n六、候选复用原则与边界\n无明显短板。",
            transcript="作者说：这首歌是最好的演唱。",
        ))

    def test_unverified_comment_fact_cannot_be_written_as_fact(self):
        self.assertFalse(self._validates("五、评论信号\n评论中有人补充某歌手此前已经有说唱创作经历。\n六、候选复用原则与边界\n无明显短板。"))

    def test_comment_section_context_covers_fact_in_later_group(self):
        analysis = (
            "五、评论信号\n"
            "1. 评论观察：多人讨论现场表现。\n"
            "2. 观点分歧：C001指出某首歌的原唱是另一位歌手，存在事实争议。\n"
            "六、候选复用原则与边界\n"
            "无明显短板。"
        )
        self.assertFalse(self._validates(analysis))
        repaired = repair_competitor_breakdown_comment_semantics(self._input(), self._payload(analysis))
        self.assertIn("待核实", repaired["analysis_text"])
        self.assertTrue(self._validates(repaired["analysis_text"]))

    def test_comment_fact_signal_without_source_marker_keeps_pending_status(self):
        signals = [{
            "signal_id": "S1",
            "signal_kind": "comment_factual",
            "signal_text": "C001指出某首歌的原唱是另一位歌手。",
            "source_anchor": "C001",
            "reason": "评论提出了母内容没有展开的新事实说法。",
        }]
        payload = self._payload(
            "五、评论信号\n评论中有人提出原唱归属争议。\n六、候选复用原则与边界\n无明显短板。",
            signals=signals,
        )
        repaired = repair_competitor_breakdown_comment_semantics(self._input(), payload)
        repaired_signal = repaired["expansion_signals"][0]
        self.assertIn("待核实", repaired_signal["signal_text"] + repaired_signal["reason"])

    def test_model_structural_inference_is_allowed(self):
        self.assertTrue(self._validates("五、评论信号\n评论中有人质疑节目规则。\n六、候选复用原则与边界\n这一段通过争议事件建立人物冲突，承担转折作用。"))

    def test_old_fact_confirmation_phrase_is_rejected_semantically(self):
        self.assertFalse(self._validates("五、评论信号\n评论中出现了对事实的确认。\n六、候选复用原则与边界\n无明显短板。"))

    def test_emotion_words_are_not_blocked_by_themselves(self):
        self.assertTrue(self._validates("五、评论信号\n评论中有人表达怀旧和共鸣，也有人说这次合作很成功。\n六、候选复用原则与边界\n无明显短板。"))


if __name__ == "__main__":
    unittest.main()
