"""讓 `python -m tools.extractor_maker <url>` 可直接執行。"""

from .cli import main

if __name__ == '__main__':
    import sys
    sys.exit(main())
