/* ==========================================================================
   Python runner — executes code blocks in the browser with Pyodide (WebAssembly).
   Each run starts from a clean namespace so every example works on its own.
   ========================================================================== */
(function () {
  "use strict";

  const PYODIDE_VERSION = "314.0.7";
  const BASE = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;

  const PRELUDE = `
import sys, builtins, io, base64, warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*JsProxy.*")          # harmless Pyodide-internal notice from threadpoolctl
import js

def _pg_input(prompt=""):
    answer = js.prompt(str(prompt))
    answer = "" if answer is None else str(answer)
    print(f"{prompt}{answer}")
    return answer
builtins.input = _pg_input

class _pg_rt:
    base = set()

    @staticmethod
    def snapshot():
        _pg_rt.base = set(sys.modules["__main__"].__dict__.keys())

    @staticmethod
    def reset():
        import gc
        d = sys.modules["__main__"].__dict__
        for k in list(d.keys()):
            if k not in _pg_rt.base:
                del d[k]
        gc.collect()

    @staticmethod
    def setup_mpl():
        import matplotlib
        matplotlib.use("agg")
        import matplotlib.pyplot as plt
        plt.show = lambda *a, **k: None
        plt.rcParams.update({
            "figure.figsize": (7, 4), "figure.dpi": 100, "axes.grid": True, "grid.alpha": 0.3,
            "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
        })

    @staticmethod
    def figures():
        if "matplotlib.pyplot" not in sys.modules:
            return []
        import matplotlib.pyplot as plt
        out = []
        for n in plt.get_fignums():
            fig = plt.figure(n)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
            out.append(base64.b64encode(buf.getvalue()).decode())
        plt.close("all")
        return out

    @staticmethod
    def display(value):
        html = None
        if hasattr(value, "_repr_html_"):
            try:
                html = value._repr_html_()
            except Exception:
                html = None
        return [repr(value), html]

_pg_rt.snapshot()
`;

  const HELP = {
    NameError: "Python does not know this name yet. Check the spelling (Python is case-sensitive), and make sure the variable or function is created <i>before</i> the line that uses it.",
    SyntaxError: "Python could not read this code. Look near the line shown for a missing <code>:</code>, an unclosed bracket <code>( [ {</code>, or a missing quote.",
    IndentationError: "The spaces at the start of a line are off. Python uses indentation (4 spaces) to know which lines belong together — after every <code>:</code> the next line must be indented.",
    TabError: "You mixed tabs and spaces for indentation. Use 4 spaces everywhere.",
    TypeError: "A value has the wrong type for this operation — for example adding a number to a string, or calling something that is not a function. Print <code>type(x)</code> to check what you really have.",
    ValueError: "The type is right but the value is not acceptable — for example <code>int(\"hello\")</code>, or unpacking 3 items into 2 names.",
    KeyError: "That key is not in the dictionary. Print <code>d.keys()</code> to see what is there, or use <code>d.get(key, default)</code> to avoid the crash.",
    IndexError: "You asked for a position that does not exist. Positions start at <b>0</b>, and the last one is <code>len(x) - 1</code>.",
    AttributeError: "This object does not have that attribute or method. Check the spelling, or run <code>dir(obj)</code> to see what it offers.",
    ZeroDivisionError: "You divided by zero. Check the value of the bottom number before dividing.",
    ModuleNotFoundError: "This package is not available in the browser. Libraries like <code>torch</code>, <code>openai</code>, <code>anthropic</code> or <code>transformers</code> need your own computer — install them with <code>pip install ...</code> and run the code locally.",
    ImportError: "The import failed. The package may not be available in the browser, or the name you are importing does not exist in it.",
    RecursionError: "A function kept calling itself without stopping. Make sure your recursive function has a base case that returns without recursing.",
    FileNotFoundError: "That file does not exist. The browser has a small private file system — create (write) the file in the same example before reading it.",
    AssertionError: "An <code>assert</code> check failed — the condition you said must be true was false.",
    StopIteration: "You called <code>next()</code> on an iterator that had nothing left.",
    UnboundLocalError: "You used a variable inside a function before giving it a value there. If you meant the outer variable, pass it in as a parameter.",
    OverflowError: "A number got too big to represent.",
    ValidationError: "Pydantic rejected the data — read the message: it lists every field that failed and why.",
    MemoryError: "The in-browser Python ran out of memory (browser memory is limited and is never handed back). <b>Reload the page</b> to start fresh — your progress is saved.",
    _ArrayMemoryError: "The in-browser Python ran out of memory while creating an array. <b>Reload the page</b> to start fresh — your progress is saved.",
  };

  let py = null;
  let booting = null;
  let mplReady = false;
  let sink = null;
  let lastSharedKey = null;
  const listeners = [];
  let state = ["idle", "Python: idle"];
  const setState = (s, label) => { state = [s, label]; listeners.forEach((f) => f(s, label)); };

  const outDec = new TextDecoder();
  const errDec = new TextDecoder();

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      if (window.loadPyodide) return resolve();
      const s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = () => reject(new Error("Could not download Python. Check your internet connection and try again."));
      document.head.appendChild(s);
    });
  }

  function boot() {
    if (booting) return booting;
    setState("loading", "Loading Python…");
    booting = (async () => {
      await loadScript(BASE + "pyodide.js");
      py = await window.loadPyodide({ indexURL: BASE });
      window.__pyodide = py;                          // handy for debugging in the browser console
      py.setStdout({ write: (buf) => { if (sink) sink.write(outDec.decode(buf, { stream: true })); return buf.length; } });
      py.setStderr({ write: (buf) => { if (sink) sink.write(errDec.decode(buf, { stream: true }), "stderr"); return buf.length; } });
      await py.runPythonAsync(PRELUDE);
      setState("ready", "Python " + py.runPython("import sys; f'{sys.version_info[0]}.{sys.version_info[1]}'") + " ready");
    })().catch((e) => {
      booting = null;
      setState("error", "Python failed to load");
      throw e;
    });
    return booting;
  }

  // `asyncio.run(main())` cannot block inside the browser — run it with top-level await instead.
  function transform(code) {
    return code.replace(/^(\s*)((?:[A-Za-z_][\w.]*\s*(?:,\s*[A-Za-z_][\w.]*\s*)*=\s*)?)asyncio\.run\((.*)\)\s*$/gm, "$1$2await $3");
  }

  function cleanTraceback(msg) {
    const lines = String(msg).split("\n");
    const i = lines.findIndex((l) => l.includes('File "<exec>"'));
    if (i === -1) {
      // SyntaxErrors are reported without a frame: keep the last few lines
      const j = lines.findIndex((l) => /^\s*File "<exec>"|^\s*line \d+/.test(l));
      return (j > -1 ? lines.slice(j) : lines.slice(-6)).join("\n").trim();
    }
    return ["Traceback (most recent call last):", ...lines.slice(i)].join("\n").trim();
  }

  const Runner = {
    name: "Python",
    canRun: (lang) => lang === "python" || lang === "py",
    onStatus(fn) { listeners.push(fn); fn(...state); },
    warm() { boot().catch(() => {}); },

    async run({ code, output, meta, lessonId }) {
      const out = output();
      out.clear();
      sink = out;
      try {
        if (!py) {
          out.write("⏳ Starting Python in your browser — the first run downloads ~10 MB and takes a few seconds…\n", "info");
          out.status("Loading Python…");
          await boot();
          out.clear();
        }
        await py.loadPackagesFromImports(code, {
          messageCallback: (m) => { if (/^Loading /.test(m)) out.status("Installing " + m.replace(/^Loading /, "").slice(0, 60) + "…"); },
          errorCallback: () => {},
        });
        if (!mplReady && /matplotlib|pyplot/.test(code)) { py.runPython("_pg_rt.setup_mpl()"); mplReady = true; }
        // Project steps share one namespace per lesson; anything else starts clean.
        const sharedKey = meta && meta.shared ? lessonId : null;
        if (!sharedKey || sharedKey !== lastSharedKey) py.runPython("_pg_rt.reset()");
        lastSharedKey = sharedKey;
        out.status("Running…");
        const t0 = performance.now();
        let result;
        try {
          result = await py.runPythonAsync(transform(code));
        } finally {
          const tail = outDec.decode();
          if (tail) out.write(tail);
        }
        const ms = performance.now() - t0;

        if (result !== undefined && result !== null) {
          const shown = py.globals.get("_pg_rt").display(result);
          const [text, html] = shown.toJs();
          shown.destroy();
          if (html) {
            const wrap = document.createElement("div");
            wrap.className = "table-wrap";
            wrap.innerHTML = html;
            wrap.querySelectorAll("script, style").forEach((n) => n.remove());
            out.node(wrap);
          } else out.write((out.empty() ? "" : "\n") + text + "\n", "result");
          if (result && typeof result.destroy === "function") result.destroy();
        }

        const figs = py.runPython("_pg_rt.figures()");
        const imgs = figs.toJs();
        figs.destroy();
        imgs.forEach((b64) => out.image("data:image/png;base64," + b64));

        if (out.empty()) out.write("(no output — add a print() to see results)\n", "info");
        out.status(`✓ Ran in ${ms < 1000 ? Math.round(ms) + " ms" : (ms / 1000).toFixed(2) + " s"}`, "ok");
      } catch (err) {
        const msg = err && err.message ? err.message : String(err);
        const tb = cleanTraceback(msg);
        out.write((out.empty() ? "" : "\n") + tb + "\n", "stderr");
        const m = tb.match(/(?:^|\n)(?:\w+\.)*(\w+(?:Error|Exception|Interrupt|Iteration))\b/g);
        const type = m ? m[m.length - 1].trim().split(".").pop() : null;
        out.status(type ? `✕ ${type}` : "✕ Error", "err");
        if (type === "NameError" && meta && meta.shared) out.help("<b>What this means:</b> this project is built step by step and this step uses something defined in an earlier step. Run the earlier steps first — or press <b>▶ Run all steps</b> at the top of the page.");
        else if (type && HELP[type]) out.help(`<b>What this means:</b> ${HELP[type]}`);
        if (py) { try { const figs = py.runPython("_pg_rt.figures()"); figs.destroy(); } catch { /* ignore */ } }
      } finally {
        sink = null;
      }
    },
  };

  window.Runner = Runner;
})();
