# calc-mam-hour

GitHub 上の活動履歴から、1 日の工数をざっくり推定する CLI ツールです。


## 前提

- `gh` CLI がインストール済みであること
- `gh auth login` または `gh auth status` が通ること

このツールは `gh api` / `gh api graphql` を使って GitHub の private repo を参照します。

## 実行方法

```bash
uv run python main.py --date 2026-04-09
```

相対日付も使えます。

```bash
uv run python main.py --date today
uv run python main.py --date yesterday
```

JSON 出力:

```bash
uv run python main.py --date 2026-04-09 --json
```

詳細ログつき:

```bash
uv run python main.py --date 2026-04-09 --verbose
```

Streamlit アプリ:

```bash
uv run streamlit run streamlit_app.py
```

## 主なオプション

- `--date`: 対象日。`YYYY-MM-DD` / `today` / `yesterday`
- `--gap-minutes`: セッションを分割する無活動時間。デフォルト `60`
- `--min-single-minutes`: セッション最小時間。デフォルト `20`
- `--event-bonus-minutes`: issue/PR 系イベントを含むセッションの固定 bonus。デフォルト `10`
- `--commit-bonus-threshold-lines`: commit bonus を付け始めるまで無視する changed lines。デフォルト `20`
- `--commit-bonus-lines-per-minute`: threshold 超過後、何 changed lines ごとに 1 分 bonus を足すか。デフォルト `25`
- `--max-commit-bonus-minutes`: 1 セッションあたりの commit bonus 上限。デフォルト `30`
- `--include-archived`: archived repo も対象に含める
- `--all-visible-repos`: 見えている private repo を全走査する
- `--sleep-seconds`: repo ごとのスキャン間隔
- `--json`: JSON 出力
- `--verbose`: stderr に詳細ログ出力

Streamlit では上記パラメータを UI から変更できます。

## どういうルールで見積もっているか

### 1. 日付の扱い

- 入力した日付は JST で解釈します
- 内部ではその JST 1 日分を UTC に変換して GitHub API に問い合わせます
- つまり `2026-04-09` を指定すると、`2026-04-09 00:00:00 JST` から `2026-04-09 23:59:59 JST` までが対象です

### 2. どの repo を調べるか

デフォルトでは、対象日に自分が触った可能性がある repo だけを先に絞り込みます。

- 自分 authored の commit がある repo
- 自分が `commenter` / `author` / `assignee` / `involves` / `reviewed-by` に該当する issue/PR が更新された repo

`--all-visible-repos` を付けた場合は、見えている private repo を広く走査します。

### 3. 何を activity として数えるか

現在カウントしているのは次の GitHub 上の可視イベントです。

- 自分 authored の commit
- issue / PR timeline 上で actor が自分の issue event
  - 例: `closed`, `referenced`, `removed_from_project_v2`
- 自分が投稿した issue comment
- 自分が投稿した PR review comment

ローカル作業だけで GitHub に痕跡が残っていない時間は数えません。

### 4. セッションの作り方

時刻順に並べたイベントを、一定時間の無活動で区切って 1 セッションにまとめます。

- イベント間の gap が `--gap-minutes` を超えたら別セッション
- デフォルトは `60` 分

例:

- 10:00 に comment
- 10:20 に commit
- 11:10 に comment

この場合、gap が 50 分以内なら同一セッションです。

### 5. セッション時間の見積もり式

各セッションの見積もり時間は次のルールです。

```text
raw_span_minutes = 最初のイベント時刻から最後のイベント時刻までの差
base_minutes = max(raw_span_minutes, min_single_minutes)
issue_bonus_minutes = event_bonus_minutes(if issue/PR 系イベントを含む)
commit_bonus_minutes = ceil(max(0, session_changed_lines - commit_bonus_threshold_lines) / commit_bonus_lines_per_minute)
commit_bonus_minutes = min(commit_bonus_minutes, max_commit_bonus_minutes)
estimated_minutes = base_minutes + issue_bonus_minutes + commit_bonus_minutes
```

デフォルト値:

- `gap_minutes = 60`
- `min_single_minutes = 20`
- `event_bonus_minutes = 10`
- `commit_bonus_threshold_lines = 20`
- `commit_bonus_lines_per_minute = 25`
- `max_commit_bonus_minutes = 30`

### 6. commit bonus の考え方

commit bonus は、セッション内 commit の `stats.total` を合計して決めます。

- `stats.total` は GitHub API の changed lines です
- additions と deletions の合計として扱います
- 最初の `20` lines は無視
- そこから `25` lines ごとに `1` 分 bonus
- ただし 1 セッションあたり最大 `30` 分まで

例:

- changed lines `10` -> commit bonus `0`
- changed lines `45` -> `1` 分
- changed lines `120` -> `4` 分
- changed lines `300` -> `12` 分
- changed lines `1000` -> 上限で `30` 分

つまり、小さい commit はほぼ base time のまま、大きい commit 群がまとまったセッションほど bonus が増えます。

### 7. repo ごとの工数配分

1 セッションに複数 repo のイベントが混ざることがあります。その場合、セッション時間をイベント数の比率で repo に按分します。

例:

- セッション推定時間が `60` 分
- repo A のイベント 2 件
- repo B のイベント 1 件

この場合:

- repo A に `40` 分
- repo B に `20` 分

### 8. 出力内容

テキスト出力では次を表示します。

- 対象日
- viewer
- scan mode
- scan した repo 数
- repo ごとの推定時間
- repo ごとの commit / issue_event 件数
- repo ごとの changed lines
- セッション一覧
- 各セッションの `base`, `issue_bonus`, `commit_bonus`, `changed_lines`
- 合計推定時間

`--json` では同じ内容を機械可読で出力します。

## 注意点

- これは GitHub 上に見えている活動だけを使った推定です
- 実験、調査、読解、ローカル検証のような非公開作業は反映されません
- diff 行数は実作業時間と完全には一致しません
- lockfile 更新、生成物、画像差し替えのような commit は diff 行数が実態とズレることがあります
- binary file を含む commit では changed lines が 0 になることがあります
- 同じ作業が複数 repo にまたがると、イベント比率で機械的に按分されます
- コメントを書かない PR review submit など、一部の活動は取りこぼす可能性があります

## Streamlit 公開時の前提

`streamlit_app.py` はブラウザ側ではなく、Streamlit サーバー側で `gh` を実行します。

- エンドユーザーの PC に `gh` は不要です
- 必要なのはデプロイ先サーバーに入った `gh` と GitHub 認証です
- このリポジトリの `packages.txt` は Streamlit Community Cloud などで `gh` を入れるためのものです
- `streamlit_app.py` は `st.secrets["GH_TOKEN"]` または `st.secrets["GITHUB_TOKEN"]` があれば `GH_TOKEN` として自動利用します
- 例は [`.streamlit/secrets.toml.example`](/LPIXEL/社内開発/calc_mam_hour/.streamlit/secrets.toml.example) に置いてあります

認証の考え方:

- 単一ユーザー運用なら、サーバーに `GH_TOKEN` を設定すれば十分です
- 対話的な `gh auth login` は公開環境では基本的に不向きです
- 複数ユーザーが各自の GitHub アカウントで使いたい場合、`gh` の共有認証ではなく GitHub OAuth を別途実装する必要があります

つまり、今の実装は「サーバーに設定された 1 つの GitHub 認証で集計する」前提です。

## 典型的な使い方

その日の工数確認:

```bash
uv run python main.py --date today
```

前日の集計を JSON 保存:

```bash
uv run python main.py --date yesterday --json > report.json
```

commit diff bonus を弱める:

```bash
uv run python main.py --date today --commit-bonus-lines-per-minute 40 --max-commit-bonus-minutes 20
```

絞り込みなしで広く走査:

```bash
uv run python main.py --date 2026-04-09 --all-visible-repos
```

Streamlit 起動:

```bash
uv run streamlit run streamlit_app.py
```
