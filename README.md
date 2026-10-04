# 分岐ビュー（JCT・IC分岐アプリ）

高速道路・自動車専用道路の分岐点を OpenStreetMap から自動で検出し、分岐の手前から分岐方向を向いたストリートビューを開けるようにする Web アプリです。走行前の予習用です。

## 構成

- `scripts/jct_streetview.py`：分岐点の検出とストリートビューの位置・向きの計算
- `.github/workflows/build-data.yml`：GitHub のクラウド上でスクリプトを実行し、結果を `docs/data/` に保存
- `docs/index.html`：スマホで見る画面（GitHub Pages で公開）
- `docs/data/sample.json`：動作確認用の架空データ

## 初回の準備

1. GitHub で新しいリポジトリを作成します（GitHub Pages を無料で使うには Public にします）。
2. このフォルダの中身をアップロードします。`.github` フォルダは隠しフォルダのため、ブラウザからのアップロードで漏れることがあります。その場合は、リポジトリの Actions タブで「set up a workflow yourself」を選び、ファイル名を `build-data.yml` にして `.github/workflows/build-data.yml` の内容を貼り付けて保存します。
3. Settings → Pages で、Source を「Deploy from a branch」、Branch を `main`、フォルダを `/docs` にして保存します。数分後に `https://ユーザー名.github.io/リポジトリ名/` で画面が開きます（最初はサンプルデータのみ）。

## 分岐データの生成

1. Actions タブ →「分岐データ生成」→「Run workflow」を押します。
2. 地域名（例：`hodogaya`）と範囲（南緯,西経,北緯,東経）を入力して実行します。初期値は保土ヶ谷周辺です。
3. 完了すると `docs/data/` にデータが追加され、数分後に画面の地域選択に表示されます。

範囲を広げすぎると OpenStreetMap のサーバーに負荷がかかり失敗するため、都市圏ごとなどに分けて実行してください。

## 任意：撮影地点の実在確認

Google Maps Platform の API キーを、Settings → Secrets and variables → Actions に `GOOGLE_MAPS_KEY` という名前で登録すると、ストリートビューの実在する撮影地点に寄せ、撮影年月も表示します（Street View メタデータ API はリクエスト無料）。

## うまくいかないとき

- 「結果を保存」の段階で権限エラーが出る場合：Settings → Actions → General → Workflow permissions を「Read and write permissions」にします。

## 注意

- 道路データ © OpenStreetMap contributors（ODbL）。表示を消さないでください。
- ストリートビューの画像は保存・転載せず、リンクで開く形にしています。
