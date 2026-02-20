# _base_stack.md - 全ジャンル共通ルール

## 技術スタック（絶対ルール）
- HTML / CSS / Vanilla JS のみ使用する
- 外部CDNは以下のみ許可:
  - Google Fonts: `https://fonts.googleapis.com`
  - Font Awesome: `https://cdnjs.cloudflare.com/ajax/libs/font-awesome/`
- バックエンド・サーバー通信は禁止
- ビルドツール不要（index.htmlをそのままブラウザで開けば動く状態を常に維持）
- フレームワーク（React, Vue等）は禁止

## ファイル構成（この形を守る）
```
workspace/
├── index.html   ← エントリーポイント。必ず存在すること
├── style.css    ← スタイル
└── main.js      ← ロジック
```
必要に応じてファイルを追加してよいが、index.htmlは必ず存在すること。

## 素材ルール
- assets/フォルダにあるファイルのみ使用可能
- ネットからの自動取得・スクレイピングは禁止
- assets/が空なら素材なしで実装する（CSSで代替する）

## 1イテレーションのスコープ
- 変更は小さく、確実に動くものにする
- 壊したら必ずそのイテレーション内で直す
- 直せないならロールバックさせる

## コードの品質
- コメントを適切に書く（日本語でも英語でも可）
- グローバル変数の乱用を避ける
- エラーハンドリングを入れる
