#!/usr/bin/env python3
"""Offline tests for jevqa.py: text-shim matching, secret masking, host allowlist, scenario validation. No browser."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jevqa  # noqa: E402


def scenario(**kw):
    base = {"base_url": "http://localhost:3002",
            "viewports": [{"name": "desktop", "width": 1440, "height": 900},
                          {"name": "mobile", "width": 390, "height": 844, "mobile": True}],
            "flows": [{"name": "home", "path": "/"}]}
    base.update(kw)
    return base


class ShimMatching(unittest.TestCase):
    VALUES = {"work email|e-?mail": "qa@example.test", "^name|full name": "Ada Test", "company": "Lantern QA Films"}

    def test_label_regex_case_insensitive(self):
        self.assertEqual(jevqa.match_value(self.VALUES, {"label": "Work EMAIL"})[0], "qa@example.test")
        self.assertEqual(jevqa.match_value(self.VALUES, {"label": "Company name"}), ("Lantern QA Films", "company"))

    def test_role_fallback(self):
        self.assertEqual(jevqa.match_value({"textbox": "x"}, {"label": "", "role": "textbox"})[0], "x")

    def test_no_match(self):
        self.assertEqual(jevqa.match_value(self.VALUES, {"label": "Budget", "role": "spinbutton"}), (None, None))

    def test_server_returns_text_or_null(self):
        shim = jevqa.Shim().start()
        try:
            shim.values = self.VALUES

            def ask(label):
                ctx = {"goal": "g", "field": {"label": label, "role": "textbox", "value": ""},
                       "page": {"title": "", "text": ""}, "recent_actions": []}
                body = json.dumps({"messages": [{"role": "system", "content": "s"},
                                                {"role": "user", "content": json.dumps(ctx)}]}).encode()
                req = urllib.request.Request(f"http://127.0.0.1:{shim.port}/v1/chat/completions", data=body,
                                             headers={"Content-Type": "application/json"})
                out = json.loads(urllib.request.urlopen(req, timeout=5).read())
                return json.loads(out["choices"][0]["message"]["content"])

            self.assertEqual(ask("Company"), {"text": "Lantern QA Films"})
            self.assertEqual(ask("Favourite colour"), {"text": None})
            self.assertEqual(shim.served[0], {"label": "Company", "value": "Lantern QA Films", "matched_pattern": "company"})
            self.assertEqual(shim.served[1]["matched_pattern"], None)
        finally:
            shim.stop()


class Masking(unittest.TestCase):
    def test_secret_labels_masked(self):
        for label in ["Password", "Card number", "CVV", "API token", "Client secret"]:
            self.assertEqual(jevqa.mask(label, "hunter2"), "***", label)

    def test_plain_labels_kept(self):
        self.assertEqual(jevqa.mask("Company", "Lantern"), "Lantern")
        self.assertIsNone(jevqa.mask("Password", None))


class HostAllowlist(unittest.TestCase):
    def test_allowlist(self):
        hosts = ["localhost", "127.0.0.1"]
        self.assertTrue(jevqa.host_allowed("http://localhost:3002/apply", hosts))
        self.assertTrue(jevqa.host_allowed("http://127.0.0.1/", hosts))
        for url in ["https://example.com/", "http://localhost.evil.com/", "file:///etc/passwd", "about:blank"]:
            self.assertFalse(jevqa.host_allowed(url, hosts), url)

    def test_flow_refused(self):
        sc, errors = jevqa.validate_scenario(scenario(flows=[{"name": "ext", "path": "https://example.com/x"},
                                                             {"name": "ok", "path": "/about"}]))
        self.assertEqual(errors, [])
        self.assertIn("allow_hosts", sc["flows"][0]["refused"])
        self.assertIsNone(sc["flows"][1]["refused"])

    def test_default_is_localhost_only(self):
        sc, _ = jevqa.validate_scenario(scenario(base_url="https://staging.example.com"))
        self.assertTrue(sc["flows"][0]["refused"])


class Validation(unittest.TestCase):
    def test_valid_defaults(self):
        sc, errors = jevqa.validate_scenario(scenario())
        self.assertEqual(errors, [])
        f = sc["flows"][0]
        self.assertEqual((f["max_steps"], f["max_slices"], f["url"]), (25, 10, "http://localhost:3002/"))

    def test_errors(self):
        bad = [
            ({"base_url": "ftp://x"}, "base_url"),
            ({"flows": []}, "flows"),
            ({"flows": [{"name": "a b", "path": "/"}]}, "name"),
            ({"flows": [{"name": "a", "path": "/"}, {"name": "a", "path": "/"}]}, "duplicate"),
            ({"flows": [{"name": "a"}]}, "path"),
            ({"flows": [{"name": "a", "path": "/", "values": {"(": "x"}}]}, "regex"),
            ({"flows": [{"name": "a", "path": "/", "max_steps": 0}]}, "max_steps"),
            ({"flows": [{"name": "a", "path": "/", "viewports": ["tablet"]}]}, "viewport"),
            ({"flows": [{"name": "a", "path": "/", "expect_text": "hi"}]}, "expect_text"),
            ({"viewports": [{"name": "x", "width": "wide", "height": 1}]}, "viewport"),
        ]
        for kw, needle in bad:
            _, errors = jevqa.validate_scenario(scenario(**kw))
            self.assertTrue(any(needle in e for e in errors), (kw, errors))


class NewKeys(unittest.TestCase):
    def test_defaults(self):
        sc, errors = jevqa.validate_scenario(scenario())
        self.assertEqual(errors, [])
        f = sc["flows"][0]
        self.assertEqual((f["wait_ms"], f["init_script"], f["wait_for"]), (0, None, None))

    def test_valid_values(self):
        sc, errors = jevqa.validate_scenario(scenario(flows=[
            {"name": "a", "path": "/", "wait_ms": 15000, "wait_for": "#hero"},
            {"name": "b", "path": "/", "wait_ms": 0}]))
        self.assertEqual(errors, [])
        self.assertEqual(sc["flows"][0]["wait_ms"], 15000)
        self.assertEqual(sc["flows"][0]["wait_for"], "#hero")

    def test_bounds_and_types(self):
        bad = [
            ({"flows": [{"name": "a", "path": "/", "wait_ms": -1}]}, "wait_ms"),
            ({"flows": [{"name": "a", "path": "/", "wait_ms": 15001}]}, "wait_ms"),
            ({"flows": [{"name": "a", "path": "/", "wait_ms": 1.5}]}, "wait_ms"),
            ({"flows": [{"name": "a", "path": "/", "wait_ms": True}]}, "wait_ms"),
            ({"flows": [{"name": "a", "path": "/", "wait_ms": "100"}]}, "wait_ms"),
            ({"flows": [{"name": "a", "path": "/", "init_script": 1}]}, "init_script"),
            ({"init_script": ["x"]}, "init_script"),
            ({"flows": [{"name": "a", "path": "/", "wait_for": ""}]}, "wait_for"),
            ({"flows": [{"name": "a", "path": "/", "wait_for": 3}]}, "wait_for"),
        ]
        for kw, needle in bad:
            _, errors = jevqa.validate_scenario(scenario(**kw))
            self.assertTrue(any(needle in e for e in errors), (kw, errors))

    def test_init_script_precedence(self):
        sc, errors = jevqa.validate_scenario(scenario(init_script="S", flows=[
            {"name": "inherit", "path": "/"},
            {"name": "own", "path": "/", "init_script": "F"},
            {"name": "off", "path": "/", "init_script": ""}]))
        self.assertEqual(errors, [])
        self.assertEqual([f["init_script"] for f in sc["flows"]], ["S", "F", None])
        self.assertEqual(jevqa.resolve_init_script(None, {}), None)
        self.assertEqual(jevqa.resolve_init_script(None, {"init_script": "F"}), "F")


class UntouchedSelects(unittest.TestCase):
    def test_detection(self):
        got = jevqa.untouched_selects([
            {"label": "Country", "value": "AF", "first": "AF"},
            {"label": "Budget", "value": "50k", "first": ""},
            {"label": "", "value": "", "first": ""},
            {"label": "Empty", "value": "", "first": None},
        ])
        self.assertEqual(got, [{"label": "Country", "value": "AF"}, {"label": "", "value": ""}])
        self.assertEqual(jevqa.untouched_selects(None), [])


class ContactSheet(unittest.TestCase):
    def test_geometry(self):
        w, h, boxes = jevqa.sheet_geometry([(1440, 900)] * 4)
        self.assertEqual(w, 6 + 3 * (480 + 6))
        self.assertEqual(boxes[:3], [(6, 6, 480, 300), (492, 6, 480, 300), (978, 6, 480, 300)])
        self.assertEqual(boxes[3], (6, 312, 480, 300))
        self.assertEqual(h, 6 + 300 + 6 + 300 + 6)

    def test_geometry_narrow_and_mixed(self):
        w, h, boxes = jevqa.sheet_geometry([(390, 844), (390, 400)])
        self.assertEqual(w, 6 + 2 * 486)
        self.assertEqual([b[3] for b in boxes], [round(844 * 480 / 390), round(400 * 480 / 390)])
        self.assertEqual(h, 12 + round(844 * 480 / 390))
        self.assertEqual(jevqa.sheet_geometry([]), (0, 0, []))

    def test_render(self):
        tmp = tempfile.mkdtemp()
        try:
            paths = []
            for i, colour in enumerate(["red", "green", "blue", "white"]):
                p = os.path.join(tmp, f"s{i}.png")
                if not _solid_png(p, 960, 600, colour):
                    self.skipTest("no Pillow or sips to generate PNGs")
                paths.append(p)
            out = jevqa.make_sheet(paths, os.path.join(tmp, "sheet.jpg"))
            try:
                from PIL import Image
            except ImportError:
                self.assertTrue(os.path.getsize(out) > 0)
                return
            im = Image.open(out).convert("RGB")
            self.assertEqual(im.size, jevqa.sheet_geometry([(960, 600)] * 4)[:2])
            px = lambda x, y: im.getpixel((x, y))
            self.assertTrue(sum(px(0, 0)) < 200)  # gutter is black (JPEG blocks bleed a little)
            r, g, b = px(6 + 240, 6 + 150)
            self.assertTrue(r > 200 and g < 60 and b < 60)         # first tile red
            r, g, b = px(978 + 240, 6 + 150)
            self.assertTrue(b > 200 and r < 60)                     # third tile blue
            self.assertTrue(all(c > 200 for c in px(6 + 240, 312 + 150)))  # fourth tile, second row, white
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NewKeysValidation(unittest.TestCase):
    def flow(self, **kw):
        return {"name": "f", "path": "/", **kw}

    def test_defaults(self):
        sc, err = jevqa.validate_scenario(scenario())
        self.assertEqual(err, [])
        f = sc["flows"][0]
        self.assertEqual((f["wait_for_loader"], f["loader_timeout_ms"], f["scroll_to"]), (True, 10000, None))

    def test_good_values_and_scenario_default(self):
        sc, err = jevqa.validate_scenario(scenario(wait_for_loader=False, flows=[
            self.flow(loader_timeout_ms=0, scroll_to="#faq"), self.flow(name="g", wait_for_loader=True)]))
        self.assertEqual(err, [])
        self.assertEqual([f["wait_for_loader"] for f in sc["flows"]], [False, True])
        self.assertEqual(sc["flows"][0]["scroll_to"], "#faq")
        self.assertEqual(sc["flows"][0]["loader_timeout_ms"], 0)

    def test_bad_values(self):
        for bad in ({"wait_for_loader": 1}, {"loader_timeout_ms": -1}, {"loader_timeout_ms": jevqa.MAX_WAIT_MS + 1},
                    {"loader_timeout_ms": True}, {"scroll_to": ""}, {"scroll_to": 3}):
            _, err = jevqa.validate_scenario(scenario(flows=[self.flow(**bad)]))
            self.assertTrue(err, bad)
        _, err = jevqa.validate_scenario(scenario(wait_for_loader="yes"))
        self.assertTrue(err)


class AgentViewport(unittest.TestCase):
    VPS = scenario()["viewports"]

    def test_first_listed(self):
        self.assertEqual(jevqa.agent_viewport({"viewports": ["mobile", "desktop"]}, self.VPS)["name"], "mobile")

    def test_scenario_first(self):
        self.assertEqual(jevqa.agent_viewport({}, self.VPS)["name"], "desktop")


class Stale(Exception):
    pass


class FakeBase:
    def __init__(self, states=None):
        self.calls, self.acts, self.evals = [], [], []
        self.states = list(states or [])
        self.is_fresh = True

    def call(self, method, **params):
        self.calls.append((method, params))
        return {}

    def fresh(self, page, action=None):
        return self.is_fresh

    def evaluate(self, expr):
        self.evals.append(expr)
        if "e.click()" in expr:
            return True
        return self.states.pop(0) if len(self.states) > 1 else (self.states[0] if self.states else None)

    def act(self, action, page, text=None):
        self.acts.append(action)
        return {"executed": action["id"]}


MOBILE = {"name": "mobile", "width": 390, "height": 844, "mobile": True}


def qa(states=None, script=None):
    return jevqa.make_browser_class(FakeBase, MOBILE, script, stale=Stale, sleep=lambda s: None)(states)


class BrowserSubclass(unittest.TestCase):
    def test_metrics_rewritten_and_passthrough(self):
        b = qa()
        b.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        b.call("Page.navigate", url="http://localhost/")
        self.assertEqual(b.calls[0], ("Emulation.setDeviceMetricsOverride",
                                      {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True}))
        self.assertEqual(b.calls[1], ("Page.navigate", {"url": "http://localhost/"}))

    def test_init_script_before_first_navigate(self):
        b = qa(script="x=1")
        b.call("Page.navigate", url="u")
        b.call("Page.navigate", url="u")
        self.assertEqual([c[0] for c in b.calls], ["Page.enable", "Page.addScriptToEvaluateOnNewDocument",
                                                   "Page.navigate", "Page.navigate"])

    def test_scroll_centre_and_screen(self):
        b = qa()
        for delta, want in ((560, 675), (-560, -675)):
            b.calls = []
            out = b.act({"id": "s", "kind": "scroll", "delta": delta}, {})
            self.assertEqual(out, {"executed": "s"})
            self.assertEqual(b.calls, [("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 195, "y": 422,
                                                                     "deltaX": 0, "deltaY": want})])
        self.assertEqual(b.after_input["id"], "s")
        self.assertEqual(b.acts, [])

    def test_scroll_stale(self):
        b = qa()
        b.is_fresh = False
        with self.assertRaises(Stale):
            b.act({"id": "s", "kind": "scroll", "delta": 560}, {})

    def click(self, b):
        return b.act({"id": "e3", "kind": "click", "node": 3, "label": "How?"}, {})

    def test_fallback_when_unchanged(self):
        b = qa([{"disclosure": True, "open": False, "expanded": None}])
        self.click(b)
        self.assertEqual(b.jevqa_fallbacks, [{"label": "How?", "node": 3}])
        self.assertTrue(any("e.click()" in e for e in b.evals))

    def test_no_fallback_when_changed(self):
        b = qa([{"disclosure": True, "open": False, "expanded": None},
                {"disclosure": True, "open": True, "expanded": None}])
        self.click(b)
        self.assertFalse(getattr(b, "jevqa_fallbacks", []))
        self.assertEqual(len(b.acts), 1)

    def test_never_for_non_disclosure(self):
        b = qa([{"disclosure": False, "open": None, "expanded": None}])
        self.click(b)
        self.assertFalse(getattr(b, "jevqa_fallbacks", []))
        self.assertFalse(any("e.click()" in e for e in b.evals))


class LoaderWait(unittest.TestCase):
    def run_wait(self, busy_polls, timeout_ms):
        t = [0.0]
        sleeps = []
        n = [0]

        def evaluate(_):
            n[0] += 1
            return {"busy": True, "reason": "overlay"} if n[0] <= busy_polls else {"busy": False, "reason": None}

        def sleep(s):
            sleeps.append(s)
            t[0] += s
        return jevqa.wait_for_loader(evaluate, timeout_ms, sleep=sleep, clock=lambda: t[0]), sleeps

    def test_clears_after_busy(self):
        rec, sleeps = self.run_wait(3, 10000)
        self.assertEqual((rec["detected"], rec["reason"], rec["cleared"]), (True, "overlay", True))
        self.assertEqual(sleeps, [0.15, 0.15, 0.15, 0.3])

    def test_not_busy(self):
        rec, sleeps = self.run_wait(0, 10000)
        self.assertEqual(rec, {"detected": False, "reason": None, "waited_ms": 0, "cleared": True})
        self.assertEqual(sleeps, [])

    def test_timeout(self):
        rec, sleeps = self.run_wait(10 ** 6, 1000)
        self.assertEqual((rec["detected"], rec["cleared"]), (True, False))
        self.assertGreaterEqual(rec["waited_ms"], 1000)
        self.assertNotIn(0.3, sleeps)


class Labels(unittest.TestCase):
    def test_format(self):
        out = jevqa.action_labels([{"id": "e7", "kind": "click", "role": "button", "label": "Menu", "value": "x"},
                                   {"id": "scroll_down", "kind": "scroll", "label": "Scroll down"},
                                   {"id": "e8", "kind": "fill", "role": "textbox", "label": "L" * 100}])
        self.assertEqual(out[0], "e7 click button: Menu")
        self.assertEqual(out[1], "scroll_down scroll: Scroll down")
        self.assertEqual(out[2], "e8 fill textbox: " + "L" * 80)

    def test_cap(self):
        self.assertEqual(len(jevqa.action_labels([{"id": f"e{i}", "kind": "click", "label": "x"}
                                                  for i in range(100)])), 60)
        self.assertEqual(jevqa.action_labels(None), [])


def _solid_png(path, w, h, colour):
    try:
        from PIL import Image
        Image.new("RGB", (w, h), colour).save(path, "PNG")
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
