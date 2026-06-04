# ブログアクセス理由分析AI

記事一覧URLを入力するだけで、ブログ記事の一覧・本文・表示アクセス数を取得し、AIが「なぜそのアクセス数なのか」を分析するStreamlitアプリです。

想定URL例

```text
https://ai-fukushi.net/archive/
```

## できること

- 記事一覧URLから記事URLを自動取得
- ページネーションを自動巡回
- 各記事ページから本文・タイトル・見出しを取得
- アプリ内部でCSV相当の表データを作成
- PV上位記事・カテゴリー別傾向を表示
- Gemini APIでアクセス数の理由を分析
- 生成したCSVを確認用にダウンロード

## ファイル構成

```text
blog_access_reason_ai/
├─ app.py
├─ requirements.txt
├─ README.md
├─ .gitignore
└─ .streamlit/
   └─ secrets.toml.example
```

## ローカルで動かす方法

### 1. 必要ファイルを配置

このフォルダを任意の場所に置きます。

### 2. 仮想環境を作成

```bash
python -m venv .venv
source .venv/bin/activate
```

Windowsの場合は以下です。

```bash
python -m venv .venv
.venv\Scripts\activate
```

### 3. ライブラリをインストール

```bash
pip install -r requirements.txt
```

### 4. Gemini APIキーを設定

`.streamlit/secrets.toml.example` をコピーして `.streamlit/secrets.toml` を作成します。

```toml
GEMINI_API_KEY = "あなたのGemini APIキー"
```

APIキーなしでも簡易分析は表示されますが、詳しいAI分析にはGemini APIキーが必要です。

### 5. アプリを起動

```bash
streamlit run app.py
```

## Streamlit Cloudにデプロイする方法

1. GitHubにこのフォルダの中身をアップロード
2. Streamlit Cloudで新規アプリを作成
3. Repository、Branch、Main file pathに `app.py` を指定
4. Settings → Secrets に以下を登録

```toml
GEMINI_API_KEY = "あなたのGemini APIキー"
```

5. Deploy

## 使い方

1. 左側の「記事一覧URL」にURLを入力
2. 最大取得記事数を指定
3. 最大巡回ページ数を指定
4. 「分析開始」を押す
5. 取得データ・PV上位記事・カテゴリー別集計を確認
6. 「全体分析を生成」または「この記事の理由をAI分析」を押す

## 注意点

- このアプリは、サイト上に表示されているアクセス数を利用します。Google AnalyticsやSearch Consoleの実データとは異なる可能性があります。
- 分析結果は、記事内容と表示PVから考えられる仮説です。原因を断定するものではありません。
- 巡回先のサイトに負荷をかけすぎないよう、アクセス間隔は通常0.5〜1秒以上にしてください。
- 本物のAPIキーはGitHubにアップロードしないでください。

## 次に追加すると便利な機能

- Google Search Console連携
- GA4連携
- 記事ごとの検索キーワード取得
- クリック率・平均掲載順位を含めた分析
- タイトル改善案の一括生成
- 似ている記事同士の比較
