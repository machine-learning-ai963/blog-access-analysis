"""
ブログアクセス理由分析AI

URLを入力するだけで、記事一覧ページを巡回し、各記事本文と表示アクセス数を取得。
アプリ内部でCSV相当のDataFrameを作成し、AIが「なぜそのアクセス数なのか」を分析します。

想定URL例:
https://ai-fukushi.net/archive/
"""

from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
import trafilatura
from bs4 import BeautifulSoup
from google import genai


# =============================
# 基本設定
# =============================

APP_TITLE = "ブログアクセス理由分析AI"
DEFAULT_ARCHIVE_URL = "https://ai-fukushi.net/archive/"
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; BlogAccessReasonAI/1.0; +https://streamlit.io/)"

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="📊",
    layout="wide",
)


# =============================
# データ型
# =============================

@dataclass
class ArchiveRecord:
    url: str
    published_date: str
    category: str
    pv: int
    archive_text: str


# =============================
# 共通関数
# =============================

def normalize_url(url: str) -> str:
    """末尾スラッシュを整える。"""
    return url.strip().rstrip("/") + "/"


def get_netloc(url: str) -> str:
    return urlparse(url).netloc.lower()


def is_same_domain(url: str, base_url: str) -> bool:
    return get_netloc(url) == get_netloc(base_url)


def build_headers() -> dict[str, str]:
    return {"User-Agent": DEFAULT_USER_AGENT}


@st.cache_data(ttl=60 * 60, show_spinner=False)
def fetch_html(url: str) -> str:
    """HTMLを取得する。1時間キャッシュ。"""
    response = requests.get(url, headers=build_headers(), timeout=25)
    response.raise_for_status()

    # 文字化け対策。requestsの推定よりHTML側のエンコードを優先しやすくする。
    if response.encoding is None or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding

    return response.text


def safe_int(text: str) -> Optional[int]:
    try:
        return int(text.replace(",", ""))
    except Exception:
        return None


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def truncate_text(text: str, limit: int = 3000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


# =============================
# 記事一覧ページの解析
# =============================

def detect_max_archive_page(html: str, archive_url: str) -> int:
    """ページネーションから最大ページ数を推定する。"""
    soup = BeautifulSoup(html, "html.parser")
    max_page = 1

    # 例: /archive/page/2/
    pattern = re.compile(r"/archive/page/(\d+)/?")

    for anchor in soup.find_all("a", href=True):
        href = urljoin(archive_url, anchor["href"])
        match = pattern.search(urlparse(href).path)
        if match:
            max_page = max(max_page, int(match.group(1)))

    # テキストだけのページ番号にも一応対応
    for text in soup.stripped_strings:
        if text.isdigit():
            num = int(text)
            if 1 <= num <= 500:
                max_page = max(max_page, num)

    return max_page


def parse_archive_anchor_text(anchor_text: str) -> Optional[dict[str, str | int]]:
    """
    アーカイブ一覧の1記事テキストから、日付・カテゴリー・PVを抜き出す。

    想定例:
    タイトル 抜粋... 2025-12-03 障害福祉 34490

    新着記事欄のように「日付だけでPVがない」ものは除外する。
    """
    text = clean_text(anchor_text)

    pattern = re.compile(
        r"(?P<date>\d{4}-\d{2}-\d{2})\s+"
        r"(?P<category>.+?)\s+"
        r"(?P<pv>\d[\d,]*)\s*$"
    )
    match = pattern.search(text)
    if not match:
        return None

    pv = safe_int(match.group("pv"))
    if pv is None:
        return None

    # PVとして現実的でないものは除外。必要ならUI側で変更可能にしてもよい。
    if pv < 0:
        return None

    return {
        "published_date": match.group("date"),
        "category": clean_text(match.group("category")),
        "pv": pv,
    }


def extract_article_records_from_archive(html: str, archive_url: str) -> list[ArchiveRecord]:
    """記事一覧HTMLから記事URL・日付・カテゴリー・PVを取得する。"""
    soup = BeautifulSoup(html, "html.parser")

    # mainがあればmain中心。なければページ全体。
    search_root = soup.find("main") or soup
    records: dict[str, ArchiveRecord] = {}

    for anchor in search_root.find_all("a", href=True):
        href = urljoin(archive_url, anchor["href"])

        if not is_same_domain(href, archive_url):
            continue

        path = urlparse(href).path
        if "/archive/" in path:
            continue
        if any(skip in path for skip in ["/category/", "/tag/", "/author/", "/privacy", "/terms"]):
            continue

        parsed = parse_archive_anchor_text(anchor.get_text(" ", strip=True))
        if not parsed:
            continue

        records[href] = ArchiveRecord(
            url=href,
            published_date=str(parsed["published_date"]),
            category=str(parsed["category"]),
            pv=int(parsed["pv"]),
            archive_text=clean_text(anchor.get_text(" ", strip=True)),
        )

    return list(records.values())


def build_archive_page_url(archive_url: str, page: int) -> str:
    if page <= 1:
        return archive_url
    return urljoin(archive_url, f"page/{page}/")


# =============================
# 記事ページの解析
# =============================

def extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        return clean_text(h1.get_text(" ", strip=True))

    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        return clean_text(og_title["content"])

    title = soup.find("title")
    if title:
        return clean_text(title.get_text(" ", strip=True))

    return ""


def extract_description(soup: BeautifulSoup) -> str:
    description = soup.find("meta", attrs={"name": "description"})
    if description and description.get("content"):
        return clean_text(description["content"])

    og_description = soup.find("meta", property="og:description")
    if og_description and og_description.get("content"):
        return clean_text(og_description["content"])

    return ""


def extract_headings(soup: BeautifulSoup) -> list[str]:
    headings: list[str] = []
    for heading in soup.find_all(["h2", "h3"]):
        text = clean_text(heading.get_text(" ", strip=True))
        if text and text not in headings:
            headings.append(text)
    return headings


def extract_article_body(html: str) -> str:
    """trafilaturaで本文抽出。失敗時はBeautifulSoupで簡易抽出。"""
    body = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )

    if body:
        return body.strip()

    soup = BeautifulSoup(html, "html.parser")
    article = soup.find("article") or soup.find("main") or soup

    # ナビ・サイドバーっぽい要素を除外
    for tag in article.find_all(["nav", "aside", "script", "style", "footer", "header"]):
        tag.decompose()

    return clean_text(article.get_text("\n", strip=True))


def extract_article_detail(url: str) -> dict[str, str | int]:
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    title = extract_title(soup)
    description = extract_description(soup)
    headings = extract_headings(soup)
    body = extract_article_body(html)

    return {
        "title": title,
        "description": description,
        "headings": " / ".join(headings[:30]),
        "heading_count": len(headings),
        "body": body,
        "body_length": len(body),
        "title_length": len(title),
    }


# =============================
# データ作成
# =============================

def add_feature_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    df["published_date"] = pd.to_datetime(df["published_date"], errors="coerce")
    df["published_year"] = df["published_date"].dt.year
    df["published_month"] = df["published_date"].dt.month
    df["published_date"] = df["published_date"].dt.strftime("%Y-%m-%d")

    df["pv_rank"] = df["pv"].rank(ascending=False, method="min").astype(int)
    df["pv_percentile"] = df["pv"].rank(pct=True)
    df["is_top_20_percent"] = df["pv_percentile"] >= 0.8
    df["is_low_20_percent"] = df["pv_percentile"] <= 0.2

    df["excerpt_from_archive"] = df["archive_text"].apply(lambda x: truncate_text(x, 180))

    return df


@st.cache_data(ttl=60 * 60, show_spinner=False)
def scrape_archive_to_dataframe(
    archive_url: str,
    max_articles: int,
    max_pages: int,
    wait_seconds: float,
) -> pd.DataFrame:
    """記事一覧を巡回し、本文も取得してDataFrame化する。"""
    archive_url = normalize_url(archive_url)
    first_html = fetch_html(archive_url)
    detected_max_page = detect_max_archive_page(first_html, archive_url)
    target_max_page = min(detected_max_page, max_pages)

    records: dict[str, ArchiveRecord] = {}

    for page in range(1, target_max_page + 1):
        page_url = build_archive_page_url(archive_url, page)
        html = first_html if page == 1 else fetch_html(page_url)

        page_records = extract_article_records_from_archive(html, archive_url)
        for record in page_records:
            records[record.url] = record

        time.sleep(wait_seconds)

        if len(records) >= max_articles:
            break

    selected_records = list(records.values())[:max_articles]

    rows: list[dict[str, str | int]] = []
    for record in selected_records:
        detail = extract_article_detail(record.url)
        rows.append(
            {
                "title": detail.get("title", ""),
                "url": record.url,
                "published_date": record.published_date,
                "category": record.category,
                "pv": record.pv,
                "description": detail.get("description", ""),
                "headings": detail.get("headings", ""),
                "heading_count": detail.get("heading_count", 0),
                "body_length": detail.get("body_length", 0),
                "title_length": detail.get("title_length", 0),
                "body": detail.get("body", ""),
                "archive_text": record.archive_text,
            }
        )
        time.sleep(wait_seconds)

    df = pd.DataFrame(rows)
    return add_feature_columns(df)


# =============================
# 分析用テキスト生成
# =============================

def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8-sig")


def make_category_stats(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    return (
        df.groupby("category", dropna=False)["pv"]
        .agg(["count", "mean", "median", "max"])
        .sort_values("mean", ascending=False)
        .round(1)
        .reset_index()
    )


def make_overall_prompt(df: pd.DataFrame) -> str:
    """全体分析用プロンプト。本文全文は入れず、要約特徴だけ入れる。"""
    df_for_prompt = df.copy()

    columns = [
        "title",
        "published_date",
        "category",
        "pv",
        "title_length",
        "body_length",
        "heading_count",
        "headings",
    ]

    top_articles = df_for_prompt.sort_values("pv", ascending=False).head(12)[columns]
    low_articles = df_for_prompt.sort_values("pv", ascending=True).head(12)[columns]
    category_stats = make_category_stats(df_for_prompt).head(12)

    summary = {
        "記事数": int(len(df_for_prompt)),
        "平均PV": round(float(df_for_prompt["pv"].mean()), 1),
        "中央値PV": round(float(df_for_prompt["pv"].median()), 1),
        "最大PV": int(df_for_prompt["pv"].max()),
        "最小PV": int(df_for_prompt["pv"].min()),
        "平均タイトル文字数": round(float(df_for_prompt["title_length"].mean()), 1),
        "平均本文文字数": round(float(df_for_prompt["body_length"].mean()), 1),
    }

    return f"""
あなたはSEO、コンテンツ編集、福祉・AI領域の記事分析に強い編集者です。
以下のブログ記事データをもとに、「なぜそのアクセス数になっているのか」を分析してください。

重要な前提:
- 表示されているPVと記事内容から考えられる仮説を出してください。
- Google検索順位、CTR、SNS流入などの実データはありません。
- そのため「断定」ではなく「可能性」「仮説」として書いてください。
- 運営者が次の記事改善に使える、具体的で実務的な日本語にしてください。

# 全体サマリー
{summary}

# カテゴリー別集計
{category_stats.to_string(index=False)}

# PV上位記事
{top_articles.to_string(index=False)}

# PV下位記事
{low_articles.to_string(index=False)}

# 出力してほしい内容
## 全体傾向
アクセスが多い記事と少ない記事の違いを説明してください。

## アクセスが多い理由の仮説
テーマ、タイトル、時事性、読者の悩み、検索需要の観点から整理してください。

## アクセスが少ない理由の仮説
テーマの狭さ、タイトルの伝わりやすさ、検索意図とのズレ、記事の長さや見出しの観点から整理してください。

## 改善案
すぐ取り組める改善案を5〜8個出してください。

## 次に書くべき記事案
アクセスを伸ばす目的で、次に書くべき記事タイトル案を10個出してください。
""".strip()


def make_article_prompt(row: pd.Series, df: pd.DataFrame) -> str:
    median_pv = int(df["pv"].median())
    avg_pv = round(float(df["pv"].mean()), 1)
    top_titles = df.sort_values("pv", ascending=False).head(8)[["title", "pv", "category"]]

    return f"""
あなたはSEO、コンテンツ編集、福祉・AI領域の記事分析に強い編集者です。
以下の1記事について、なぜこのアクセス数になっているのかを分析してください。

重要な前提:
- 実データは記事内容と表示PVのみです。
- 原因は断定せず、仮説として書いてください。
- 改善に使える具体的な文章にしてください。

# 対象記事
タイトル: {row.get('title', '')}
URL: {row.get('url', '')}
投稿日: {row.get('published_date', '')}
カテゴリー: {row.get('category', '')}
PV: {row.get('pv', '')}
全体平均PV: {avg_pv}
中央値PV: {median_pv}
タイトル文字数: {row.get('title_length', '')}
本文文字数: {row.get('body_length', '')}
見出し数: {row.get('heading_count', '')}
メタ説明: {row.get('description', '')}
見出し: {row.get('headings', '')}
本文冒頭: {truncate_text(str(row.get('body', '')), 4000)}

# 比較用 PV上位記事
{top_titles.to_string(index=False)}

# 出力してほしい内容
## 評価
この記事は全体の中で伸びているか、伸びていないかを説明してください。

## アクセス数になった理由の仮説
タイトル、検索需要、本文内容、時事性、読者の悩みとの一致度から説明してください。

## 改善案
タイトル改善案、見出し改善案、本文に足すと良い内容を出してください。

## リライト後タイトル案
5案出してください。
""".strip()


def get_gemini_api_key() -> str:
    """Streamlit Secrets → 環境変数の順に取得。"""
    try:
        key = st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        key = ""

    if not key:
        import os
        key = os.environ.get("GEMINI_API_KEY", "")

    return key


def generate_with_gemini(prompt: str, model_name: str, api_key: str) -> str:
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
    )
    return response.text or ""


def make_rule_based_report(df: pd.DataFrame) -> str:
    """APIキーがない場合でも使える簡易分析。"""
    if df.empty:
        return "分析できる記事データがありません。"

    category_stats = make_category_stats(df).head(5)
    top = df.sort_values("pv", ascending=False).head(5)
    low = df.sort_values("pv", ascending=True).head(5)

    avg_pv = round(float(df["pv"].mean()), 1)
    median_pv = round(float(df["pv"].median()), 1)

    top_category_text = "、".join(
        f"{row['category']} 平均{row['mean']}PV"
        for _, row in category_stats.iterrows()
    )

    top_title_text = "\n".join(
        f"- {row['title']}（{row['pv']}PV）"
        for _, row in top.iterrows()
    )

    low_title_text = "\n".join(
        f"- {row['title']}（{row['pv']}PV）"
        for _, row in low.iterrows()
    )

    return f"""
## 簡易分析結果

取得記事数は **{len(df)}件**、平均PVは **{avg_pv}**、中央値PVは **{median_pv}** です。

## PV上位記事
{top_title_text}

## PV下位記事
{low_title_text}

## カテゴリー傾向
{top_category_text}

## アクセスが多い記事の仮説
PV上位記事は、タイトルだけで読者の悩みや知りたいことが伝わりやすい傾向があります。特に、制度改正、AI活用、具体的な手順、実践方法など、検索されやすいテーマはアクセスが伸びやすい可能性があります。

## アクセスが少ない記事の仮説
PV下位記事は、テーマが狭い、検索する人が少ない、タイトルから得られる情報がやや分かりにくい、公開から時間が短いなどの要因が考えられます。

## 改善案
- タイトルに「誰向けの記事か」を入れる
- タイトルに「何がわかるか」を具体的に入れる
- 冒頭で読者の悩みを明確にする
- 見出しに検索されやすい言葉を入れる
- PV上位記事から内部リンクを貼る
- 制度改正や最新情報系の記事は公開後に定期更新する

※これはAPIキーなしの簡易分析です。Gemini APIキーを設定すると、より詳しい文章分析ができます。
""".strip()


# =============================
# UI
# =============================

st.title("📊 ブログアクセス理由分析AI")
st.caption("URLを入力するだけで記事一覧と本文を取得し、アクセス数の理由を分析します。")

with st.sidebar:
    st.header("設定")

    archive_url = st.text_input(
        "記事一覧URL",
        value=DEFAULT_ARCHIVE_URL,
        help="例: https://ai-fukushi.net/archive/",
    )

    max_articles = st.slider(
        "最大取得記事数",
        min_value=5,
        max_value=300,
        value=50,
        step=5,
    )

    max_pages = st.slider(
        "最大巡回ページ数",
        min_value=1,
        max_value=50,
        value=13,
        step=1,
    )

    wait_seconds = st.slider(
        "アクセス間隔（秒）",
        min_value=0.0,
        max_value=3.0,
        value=0.8,
        step=0.1,
        help="サイトに負荷をかけすぎないよう、通常は0.5〜1.0秒以上を推奨します。",
    )

    model_name = st.selectbox(
        "Geminiモデル",
        options=["gemini-2.5-flash", "gemini-2.5-pro"],
        index=0,
    )

    st.divider()
    st.write("APIキーは `.streamlit/secrets.toml` または Streamlit Cloud の Secrets に設定します。")

run_button = st.button("分析開始", type="primary")

if run_button:
    if not archive_url.strip():
        st.error("記事一覧URLを入力してください。")
        st.stop()

    try:
        archive_url = normalize_url(archive_url)

        progress_text = st.empty()
        progress_text.info("記事一覧と本文を取得しています。記事数が多い場合は少し時間がかかります。")

        with st.spinner("記事データを取得中..."):
            df = scrape_archive_to_dataframe(
                archive_url=archive_url,
                max_articles=max_articles,
                max_pages=max_pages,
                wait_seconds=wait_seconds,
            )

        progress_text.empty()

    except requests.HTTPError as e:
        st.error(f"ページ取得でHTTPエラーが発生しました: {e}")
        st.stop()
    except requests.RequestException as e:
        st.error(f"ページ取得に失敗しました: {e}")
        st.stop()
    except Exception as e:
        st.error(f"データ取得中にエラーが発生しました: {e}")
        st.stop()

    if df.empty:
        st.error("記事データを取得できませんでした。URLやページ構造を確認してください。")
        st.stop()

    st.session_state["df"] = df
    st.success(f"{len(df)}件の記事を取得しました。")


if "df" not in st.session_state:
    st.info("左側の設定を確認して、分析開始ボタンを押してください。")
    st.stop()


df = st.session_state["df"]

# =============================
# ダッシュボード
# =============================

col1, col2, col3, col4 = st.columns(4)
col1.metric("取得記事数", f"{len(df)}件")
col2.metric("平均PV", f"{df['pv'].mean():.1f}")
col3.metric("中央値PV", f"{df['pv'].median():.1f}")
col4.metric("最大PV", f"{df['pv'].max():,}")

csv_bytes = dataframe_to_csv_bytes(df)
st.download_button(
    label="内部生成CSVをダウンロード",
    data=csv_bytes,
    file_name="blog_access_analysis.csv",
    mime="text/csv",
)


tab_overall, tab_articles, tab_data = st.tabs(["全体分析", "記事別分析", "取得データ"])

with tab_overall:
    st.subheader("PV上位記事")
    top_chart_df = df.sort_values("pv", ascending=False).head(15).copy()
    st.bar_chart(top_chart_df.set_index("title")["pv"])

    st.subheader("カテゴリー別集計")
    category_stats = make_category_stats(df)
    st.dataframe(category_stats, use_container_width=True)

    st.subheader("AIによる全体分析")
    api_key = get_gemini_api_key()

    if st.button("全体分析を生成", type="secondary"):
        if api_key:
            prompt = make_overall_prompt(df)
            with st.spinner("AIが全体傾向を分析しています..."):
                try:
                    report = generate_with_gemini(prompt, model_name, api_key)
                except Exception as e:
                    st.warning(f"Geminiでの分析に失敗しました。簡易分析を表示します。詳細: {e}")
                    report = make_rule_based_report(df)
        else:
            report = make_rule_based_report(df)

        st.session_state["overall_report"] = report

    if "overall_report" in st.session_state:
        st.markdown(st.session_state["overall_report"])
    else:
        st.caption("全体分析を生成ボタンを押すと、アクセス数の理由を分析します。")


with tab_articles:
    st.subheader("記事別に理由を分析")

    sorted_df = df.sort_values("pv", ascending=False).reset_index(drop=True)
    options = [f"{row['pv']}PV｜{row['title']}" for _, row in sorted_df.iterrows()]
    selected_label = st.selectbox("分析したい記事", options=options)
    selected_index = options.index(selected_label)
    selected_row = sorted_df.iloc[selected_index]

    col_a, col_b, col_c = st.columns(3)
    col_a.metric("PV", f"{int(selected_row['pv']):,}")
    col_b.metric("PV順位", f"{int(selected_row['pv_rank'])}位")
    col_c.metric("本文文字数", f"{int(selected_row['body_length']):,}")

    st.write(f"**タイトル**: {selected_row['title']}")
    st.write(f"**URL**: {selected_row['url']}")
    st.write(f"**投稿日**: {selected_row['published_date']} / **カテゴリー**: {selected_row['category']}")

    with st.expander("取得した本文の冒頭を確認"):
        st.write(truncate_text(str(selected_row.get("body", "")), 2000))

    if st.button("この記事の理由をAI分析", type="secondary"):
        api_key = get_gemini_api_key()
        if api_key:
            prompt = make_article_prompt(selected_row, df)
            with st.spinner("AIがこの記事を分析しています..."):
                try:
                    article_report = generate_with_gemini(prompt, model_name, api_key)
                except Exception as e:
                    article_report = f"Geminiでの分析に失敗しました。詳細: {e}"
        else:
            if selected_row["pv"] >= df["pv"].median():
                article_report = "この記事は中央値以上のPVです。タイトルに検索されやすいテーマや具体性があり、読者の課題に合っている可能性があります。Gemini APIキーを設定すると、本文内容まで踏まえて詳しく分析できます。"
            else:
                article_report = "この記事は中央値未満のPVです。テーマの検索需要、タイトルの具体性、本文と検索意図の一致度を見直す余地があります。Gemini APIキーを設定すると、本文内容まで踏まえて詳しく分析できます。"

        st.session_state["article_report"] = article_report

    if "article_report" in st.session_state:
        st.markdown(st.session_state["article_report"])


with tab_data:
    st.subheader("取得データ")
    display_columns = [
        "title",
        "url",
        "published_date",
        "category",
        "pv",
        "pv_rank",
        "title_length",
        "body_length",
        "heading_count",
        "description",
        "headings",
    ]
    st.dataframe(df[display_columns], use_container_width=True)

    with st.expander("全カラムを表示"):
        st.dataframe(df, use_container_width=True)
