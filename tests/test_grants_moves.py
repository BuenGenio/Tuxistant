"""A skill moved, copied or broken by hand never ends up allowed more than it was without being
taken out: the cases the last review of 0.5.0 found."""
import json
import os
import shutil
from unittest import mock

from tests import test_recipes as tr
from tests.test_recipes import EXAMPLES
from tuxistant import skillhost
from tuxistant.skillhost import SkillHost

CODE = ("id: org.example.code\nname: C\ndescription: C\nrun: [python3, m.py]\n"
        "actions:\n  code.a:\n    description: A\n")
CODE_NET = ("id: org.example.code\nname: C\ndescription: C\nrun: [python3, m.py]\n"
            "permissions:\n  network: true\nactions:\n  code.a:\n    description: A\n")


class Process:
    async def stop(self, graceful=True):
        pass


class Moves(tr.Case):
    def grants(self):
        return self.user.parent / skillhost.GRANTS

    def record(self, skill_id):
        return json.loads(self.grants().read_text()).get(skill_id)

    def test_a_code_skill_moved_onto_an_empty_folder_is_not_taken_out(self):
        self.host.launcher = lambda *_: Process()
        self.example("work.yaml")
        code = self.user / "code"
        code.mkdir()
        (code / "m.py").write_text("")
        (code / "skill.yaml").write_text(CODE)
        (self.user / "drafts").mkdir()     # an empty folder, failing since before
        self.host.reload()
        (code / "skill.yaml").unlink()
        self.host.reload()
        os.rename(code, self.user / "drafts")
        self.host.reload()
        self.assertFalse(self.record("org.example.code").get("removed"))
        (self.user / "drafts" / "skill.yaml").write_text(CODE_NET)
        with mock.patch.object(SkillHost, "_clear_state"):
            problems = self.host.reload()
        self.assertIn("network", problems[str(self.user / "drafts")])
        self.assertNotIn("code.a", self.host.current().actions)
        self.assertEqual(self.record("org.example.code")["permissions"], {})

    def test_taking_out_a_copy_that_stood_in_still_holds_the_original_back(self):
        self.example("work.yaml", "coins.yaml")
        (self.user / "coins.yaml").rename(self.user / "b.yaml")
        (self.user / "b.yaml").write_text("id: [broken coins")
        self.host.reload()
        shutil.copy(EXAMPLES / "coins.yaml", self.user / "c.yaml")
        self.host.reload()
        (self.user / "c.yaml").unlink()
        self.host.reload()
        tr.Host.coins_evil(self, "b.yaml")
        problems = self.host.reload()
        self.assertIn("evil.example", problems[str(self.user / "b.yaml")])
        self.assertNotIn("coins.leak", self.host.current().actions)

    def test_grants_fixed_after_a_failed_reload_count_at_the_next(self):
        self.example("work.yaml", "coins.yaml")
        good = self.grants().read_text()
        self.grants().write_text("[broken")
        gone, calls = skillhost._Grants.gone, []

        def boom(grants, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return gone(grants, *args, **kwargs)
        with mock.patch.object(skillhost._Grants, "gone", boom):
            self.host.reload()
            self.grants().write_text(good)
            problems = self.host.reload()
        self.assertFalse(self.host.grants.broken)
        self.assertNotIn(str(self.user / "coins.yaml"), problems)
        self.assertIn("coins.price", self.host.current().actions)
