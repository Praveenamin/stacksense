"""Compile sensitive modules to C extensions (.so) for SEALED distribution.

Why: a client-side license check that ships as readable .py is trivially patched
(`has_feature` -> `return True`). Compiling the licensing logic to a C extension and
deleting the .py source removes the easy edit point — the entitlement logic ships as
a binary `.so`, not source.

This runs ONLY inside the sealed image build (see Dockerfile.sealed). The dev tree keeps
the .py (interpreted as normal); the sealed image keeps only the compiled .so. When both
would exist, Python loads the .so, so behaviour is identical — it's the same code, compiled.

Add more enforcement modules to MODULES to spread the checks (no single chokepoint).
Keep each listed module self-contained enough to compile cleanly.
"""
import glob
import os

from setuptools import setup
from Cython.Build import cythonize

# Modules compiled to .so (their .py + generated .c are removed afterwards).
MODULES = [
    "core/licensing.py",
]

if __name__ == "__main__":
    setup(
        name="stacksense_sealed",
        # annotation_typing=False: treat PEP-484 annotations as plain Python (do NOT let
        # Cython turn `max_servers: int | None` etc. into C-level types) — required for
        # dataclasses / Django code to compile and behave identically.
        ext_modules=cythonize(
            MODULES,
            compiler_directives={"language_level": "3", "annotation_typing": False},
            quiet=True,
        ),
        script_args=["build_ext", "--inplace"],
    )

    for src in MODULES:
        base = src[:-3]  # strip ".py"
        for junk in (src, base + ".c"):
            if os.path.exists(junk):
                os.remove(junk)
        built = glob.glob(base + ".*.so")
        if not built:
            raise SystemExit(f"sealed build FAILED: no .so produced for {src}")
        print(f"sealed: {src} -> {built[0]} (source removed)")
