import sys

from .app import main

if __name__ == "__main__":
    # 戻り値を終了コードにする（-p の outcome が exit code になる約束・設計 §11）。
    sys.exit(main())
