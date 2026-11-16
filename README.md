# Project Portfolio Map (`project-management`)

全事業プロジェクトの構成・進捗（`PROGRESS.md`）・外部KPI（YouTube / Cloudflare）・開発＆運用ハーネス体制を統合管理する独立コックピットです。

## 構成ファイル
- `index.html`: ミニマル Bento UI ダッシュボード（3モード切替 + Mermaid.js v11 構成図）
- `portfolio.json`: `sync_portfolio.py` により毎時自動生成される単一データソース（SSOT）
- `sync_portfolio.py`: ローカルスキャン（`PROGRESS.md`, `MILESTONES.md`, Git, ハーネス状況）および外部API（YouTube Data API v3 / Cloudflare Web Analytics GraphQL API）取得・Push スクリプト

## 自動同期
Windows Task Scheduler（`Antigravity_Cockpit_Hourly_Sync`）から毎時実行され、`portfolio.json` を更新して GitHub Pages（`main` / `gh-pages`）へ自動デプロイします。

