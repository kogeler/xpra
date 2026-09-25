# Copyright (C) 2026 kogeler
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import contrib
import knowledge
from test_artifacts import RECORD


class KnowledgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.state = contrib.cleanup_state_root(self.repo)
        self.state.parent.mkdir(mode=0o700)

    def write(self, session: str, text: str = RECORD) -> Path:
        sessions = self.state / knowledge.ROOT / knowledge.SESSIONS
        sessions.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = sessions / f"{session}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def problems(self) -> dict[str, str]:
        return {str(path): reason for path, reason in knowledge.inspect(self.state)[1]}

    def test_absent_knowledge_is_valid(self) -> None:
        self.assertEqual(knowledge.inspect(self.state), ((), ()))
        self.assertEqual(knowledge.write_index(self.repo), 0)

    def test_registry_is_generated_newest_first_and_checked(self) -> None:
        self.write("popup-20260912")
        self.write("refresh-20260920", RECORD.replace("2026-09-12", "2026-09-20").replace("patch", "refresh", 1))
        self.assertIn("missing", self.problems()["knowledge/INDEX.md"])
        self.assertEqual(knowledge.write_index(self.repo), 2)
        index = (self.state / knowledge.ROOT / knowledge.INDEX).read_text(encoding="utf-8")
        rows = [line for line in index.splitlines() if line.startswith("| [")]
        self.assertEqual([row.split("]")[0] for row in rows], ["| [refresh-20260920", "| [popup-20260912"])
        self.assertIn("| example-popup-grab | gtk3, popup, modal grab |", rows[1])
        self.assertEqual(self.problems(), {})
        (self.state / knowledge.ROOT / knowledge.INDEX).write_text(index + "| hand | edit |\n", encoding="utf-8")
        self.assertIn("hand-edited", self.problems()["knowledge/INDEX.md"])

    def test_new_record_is_a_template_that_fails_until_filled(self) -> None:
        path = knowledge.new_record(self.repo, "wayland-keymap-20260924")
        self.assertIn("knowledge/sessions/wayland-keymap-20260924.md", self.problems())
        text = path.read_text(encoding="utf-8")
        for placeholder, value in (
            ("<one-line problem statement>", "Keymap drift"),
            (f"<{'|'.join(knowledge.KINDS)}>", "investigation"),
            ("<comma-separated case slugs, or none>", "none"),
            ("<comma-separated symptoms, error strings, subsystems, symbols>", "keymap"),
            ("<one sentence: the conclusion a future agent needs>", "Filled."),
        ):
            self.assertIn(placeholder, text)
            text = text.replace(placeholder, value)
        path.write_text(knowledge.COMMENT_RE.sub("Filled.", text), encoding="utf-8")
        self.assertEqual(knowledge.parse_record(path).kind, "investigation")
        with self.assertRaises(FileExistsError):
            knowledge.new_record(self.repo, "wayland-keymap-20260924")
        with self.assertRaises(contrib.ContribError):
            knowledge.new_record(self.repo, "Not A Slug")

    def test_record_contract(self) -> None:
        invalid = {
            "no-title": RECORD.replace("# Popup keeps a modal grab", "Popup"),
            "placeholder-title": RECORD.replace("# Popup keeps a modal grab", "# <one-line problem statement>"),
            "bad-date": RECORD.replace("2026-09-12", "2026-13-40"),
            "bad-kind": RECORD.replace("- Kind: patch", "- Kind: misc"),
            "bad-case": RECORD.replace("example-popup-grab", "Client Popup"),
            "empty-keyword": RECORD.replace("gtk3, popup", "gtk3, , popup"),
            "pipe": RECORD.replace("before unmap.", "before | unmap."),
            "placeholder": RECORD.replace("- Summary: The", "- Summary: <one sentence> The"),
            "long-summary": RECORD.replace("before unmap.", "x" * knowledge.MAX_SUMMARY_CHARACTERS),
            "missing-field": RECORD.replace("- Keywords: gtk3, popup, modal grab\n", ""),
            "missing-section": RECORD.replace("## Findings", "## Notes"),
            "reordered": RECORD.replace("## Problem", "## Tmp").replace("## Findings", "## Problem"),
            "empty-section": RECORD.replace("The release ran after unmap.", "<!-- fill -->"),
            "oversized": RECORD + "x" * knowledge.MAX_RECORD_BYTES,
        }
        for session, text in invalid.items():
            with self.subTest(session=session):
                path = self.write(session, text)
                with self.assertRaises(contrib.ContribError):
                    knowledge.parse_record(path)
                path.unlink()
        record = knowledge.parse_record(self.write("valid", RECORD + "\n## Extra\n\nAllowed.\n"))
        self.assertEqual((record.kind, record.cases), ("patch", ("example-popup-grab",)))
        unowned = self.write("none", RECORD.replace("example-popup-grab", "none"))
        self.assertEqual(knowledge.parse_record(unowned).cases, ())

    def test_only_records_and_registry_may_live_in_knowledge(self) -> None:
        self.write("popup-20260912")
        knowledge.write_index(self.repo)
        for name in ("raw.log", "sessions/extra.txt", "sessions/nested"):
            with self.subTest(name=name):
                path = self.state / knowledge.ROOT / name
                if name.endswith("nested"):
                    path.mkdir()
                else:
                    path.write_text("raw\n", encoding="utf-8")
                self.assertIn(f"knowledge/{name}", self.problems())
                with self.assertRaises(contrib.ContribError):
                    knowledge.write_index(self.repo)
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()
        self.assertEqual(self.problems(), {})

    def test_record_links_are_rejected(self) -> None:
        target = self.write("popup-20260912")
        (target.parent / "link.md").symlink_to(target)
        self.assertIn("knowledge/sessions/link.md", self.problems())


if __name__ == "__main__":
    unittest.main(verbosity=2)
