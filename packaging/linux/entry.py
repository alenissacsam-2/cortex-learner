"""The frozen app's entry (keepfold.spec): `keepfold-app` runs the app and `keepfold` the CLI,
as pyproject's scripts do. It goes by the program's own file name, so both share one analysis."""

import sys
from pathlib import Path

if Path(sys.executable).name == "keepfold-app":
    from keepfold.app.launch import main
else:
    from keepfold.taint import main
sys.exit(main())
