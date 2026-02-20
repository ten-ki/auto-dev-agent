"""
auto-dev-agent / run.py
使い方: python run.py --brief path/to/brief.txt
"""

import argparse
import sys
from pathlib import Path
from bootstrap import Bootstrap
from orchestrator import Orchestrator


def main():
    parser = argparse.ArgumentParser(description="AI自律開発エージェント")
    parser.add_argument("--brief", required=True, help="brief.txtのパス")
    parser.add_argument("--iterations", type=int, default=0, help="最大イテレーション数（0=無限）")
    parser.add_argument("--interval", type=int, default=60, help="イテレーション間隔（秒）")
    args = parser.parse_args()

    brief_path = Path(args.brief)
    if not brief_path.exists():
        print(f"[ERROR] brief.txtが見つかりません: {brief_path}")
        sys.exit(1)

    print("=" * 60)
    print("  AI自律開発エージェント 起動")
    print("=" * 60)

    # ブートストラップ: brief.txt → spec.md / status.md 生成
    bootstrap = Bootstrap(brief_path)
    project = bootstrap.run()

    print(f"\n[OK] プロジェクト初期化完了: {project['name']}")
    print(f"[OK] 作業ディレクトリ: {project['project_dir']}")
    print(f"\n自律イテレーション開始...\n")

    # オーケストレーターでイテレーションを回す
    orchestrator = Orchestrator(project, max_iterations=args.iterations, interval=args.interval)
    orchestrator.run()


if __name__ == "__main__":
    main()
