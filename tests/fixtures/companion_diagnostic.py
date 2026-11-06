"""Run the real CLI, retaining stacks if a fictional smoke child stalls."""

import faulthandler
import runpy
import sys

faulthandler.enable(file=sys.stderr)
faulthandler.dump_traceback_later(5, repeat=True, file=sys.stderr)
try:
    sys.argv[0] = "clytie"
    runpy.run_module("clytie", run_name="__main__")
finally:
    faulthandler.cancel_dump_traceback_later()
