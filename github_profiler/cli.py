"""CLI interface for GitHub Profiler"""

import argparse
import os

from dotenv import load_dotenv


def parse_args() -> argparse.Namespace:
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="GitHub Developer Intelligence System")
    parser.add_argument("--username", required=True, help="GitHub username")
    parser.add_argument("--since", default="2023-01-01", help="Scan from date")
    parser.add_argument("--output", default="profile.json", help="Output file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    return parser.parse_args()


def main() -> None:
    """Main entry point"""
    load_dotenv()
    args = parse_args()

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("❌ GITHUB_TOKEN missing in .env")
        print("   Please create .env file with: GITHUB_TOKEN=your_token_here")
        return

    print("🚀 GitHub Developer Intelligence System")
    print(f"📊 Target: {args.username}")
    print(f"📅 Since: {args.since}")
    print(f"💾 Output: {args.output}")
    print()
    print("⚠️  Phase 0 complete. Awaiting Phase 1 implementation...")
    print("   (Tree collector, commit collector, etc.)")


if __name__ == "__main__":
    main()
