"""제공사를 전환해도 인증키·모델·이어하기 설정이 섞이지 않는지 검사한다."""
import os
import types
import unittest
from unittest import mock

from test_ops import gui_module


class Var:
    def __init__(self, value): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value


class Widget:
    def __init__(self): self.state = None
    def configure(self, **kw): self.state = kw.get("state", self.state)


class ProviderSettings(unittest.TestCase):
    def test_legacy_config_preserves_gemini_key_and_does_not_share_it(self):
        with gui_module() as gui:
            cfg = gui.upgrade_config(dict(gui.DEFAULTS, model="gemini-custom", api_key="legacy-key", second_model="gemini-fast"))
            self.assertEqual(gui.provider_profile(cfg, "gemini"), {"model": "gemini-custom", "api_key": "legacy-key", "second_model": "gemini-fast"})
            self.assertEqual(gui.provider_profile(cfg, "openai")["api_key"], "")
            gui.remember_provider(cfg, "openai", "gpt-custom", "openai-key", "gpt-small")
            cfg["reader"] = "openai"
            gui.upgrade_config(cfg)
            self.assertEqual((cfg["model"], cfg["api_key"]), ("gpt-custom", "openai-key"))
            self.assertEqual(gui.provider_profile(cfg, "gemini")["api_key"], "legacy-key")

    def test_switch_roundtrip_restores_models_keys_and_preserves_resume_flag(self):
        with gui_module() as gui:
            app = types.SimpleNamespace(cfg={}, _active_reader="gemini", running=False,
                v_reader=Var("openai"), v_model=Var("gemini-custom"), v_key=Var("gemini-key"), v_model2=Var("gemini-fast"),
                v_provider=Var("Gemini"), v_force=Var(False), cb_provider=Widget(), e_model=Widget(), e_key=Widget(), e_model2=Widget(),
                _gemini_widgets=[(Widget(), "readonly")])
            def save(quiet=False):
                gui.remember_provider(app.cfg, app.v_reader.get(), app.v_model.get(), app.v_key.get(), app.v_model2.get())
            app.save_settings = save
            gui.App._reader_changed(app)
            self.assertEqual(app.v_key.get(), "")
            self.assertTrue(app.v_model.get().startswith("gpt-"))
            self.assertEqual(app._gemini_widgets[0][0].state, "disabled")
            app.v_key.set("openai-key"); app.v_model.set("gpt-custom"); app.v_model2.set("gpt-small")
            app.v_reader.set("gemini"); gui.App._reader_changed(app)
            self.assertEqual((app.v_key.get(), app.v_model.get(), app.v_model2.get()), ("gemini-key", "gemini-custom", "gemini-fast"))
            app.v_reader.set("openai"); gui.App._reader_changed(app)
            self.assertEqual((app.v_key.get(), app.v_model.get(), app.v_model2.get()), ("openai-key", "gpt-custom", "gpt-small"))
            self.assertFalse(app.v_force.get())
            app.running = True; gui.App._reader_changed(app)
            self.assertEqual((app.cb_provider.state, app.e_key.state, app.e_model.state), ("disabled", "disabled", "disabled"))

    def test_prepare_env_sets_only_selected_provider_key(self):
        with gui_module() as gui, mock.patch.dict(os.environ, {"GEMINI_API_KEY": "original-gemini", "OPENAI_API_KEY": "old-openai"}, clear=True):
            saved = []
            app = types.SimpleNamespace(v_reader=Var("anthropic"), v_key=Var("sk-ant-test"), v_maxcalls=Var("0"), save_settings=lambda **kw: saved.append(True))
            self.assertTrue(gui.App._prepare_env(app))
            self.assertEqual(os.environ["GEMINI_API_KEY"], "original-gemini")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "old-openai")
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-ant-test")
            self.assertTrue(saved)
            self.assertFalse(set(gui.SECRET_ENV) & set(gui.child_env()))

    def test_build_passes_selected_provider_model_and_call_cap(self):
        with gui_module() as gui:
            calls = []
            app = types.SimpleNamespace(_need_pdf=lambda: True, _prepare_env=lambda: True,
                v_reader=Var("anthropic"), v_model=Var("claude-custom"), pdf=lambda: "book.pdf", work=lambda: "work",
                _pages=lambda: [], _build_opts=lambda: [], _cap_opts=lambda: ["--max-calls", "12"],
                _with_plan=lambda kind, go: go([]), start=lambda argv, label: calls.append(argv), _save_book=lambda create=False: None,
                v_skipany=Var(False), v_ctxmodel=Var(True))
            app._answered_opts = gui.App._answered_opts.__get__(app)
            gui.App.do_build(app)
            from yeone_ocr.cli import build_parser
            args = build_parser().parse_args(calls[0])
            self.assertEqual((args.reader, args.model, args.max_calls, args.skip_answered), ("anthropic", "claude-custom", 12, "same-model"))


if __name__ == "__main__":
    unittest.main()
