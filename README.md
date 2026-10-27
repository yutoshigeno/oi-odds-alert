# 大井競馬・直前オッズ通知（無料枠・試作）

## できること

- NAR公式の当日CSV/ZIP（race, odds）を読み込む
- 大井競馬の当日の開催レースを自動抽出する
- 発走10〜20分前は「事前予想」、1〜10分前は「直前予想」を紙上で再計算する
- オッズに応じて買い目候補を更新し、Discordに最大2点の単勝候補と各100円の**紙上買い目**を通知する
- オッズが古い場合、出走馬データが不足する場合は安全側に停止・見送り
- GitHub Actionsで5分ごとに起動（Macを起動しっぱなしにしなくてOK）

**注意：** 初期予想は未学習の試作指数です。馬の強さの推定精度も回収率も検証していません。オッズ×指数は期待回収率ではありません。勝てる仕組みではなく、自動監視・通知の基盤です。投票機能はありません。GitHub定時実行は遅延・スキップの可能性があり、発走3分前の通知を保証しません。

## データ元（公式）

- [NAR当日のレースデータ](https://www.keiba.go.jp/KeibaWeb/DataDownload/RaceDataDownload?type=daily)
- [NAR当日のオッズデータ](https://www.keiba.go.jp/KeibaWeb/DataDownload/OddsDataDownload?type=daily)
- [公式CSV説明書](https://www.keiba.go.jp/pdf/manual/data_pdf_manual.pdf)

公式は **約2分に1回程度** の当日ZIP取得を推奨。本テンプレートは原則5分間隔。サイト利用規約も確認してください。2つのURLはZIPを返すことを外部確認していますが、実行環境からの取得・ZIP解析のライブ動作は未検証です。

## GitHub Actions セットアップ

1. Discordサーバーの任意のチャンネル → **チャンネルを編集** → **連携サービス（Integrations）** → **ウェブフック** → 新規作成 → URLをコピー。Webhook URLは他人に教えない。
2. GitHubで新規リポジトリ `oi-odds-alert` を作る。**追加料金ゼロを優先するならPublic**（コードと紙上予想履歴は公開されます）。**予想履歴を非公開にしたいならPrivate**（無料枠・請求上限の管理が必要）。ZIPの中身をルートにアップロード（`.github/workflows/monitor.yml` の階層を維持）。
3. GitHubのリポジトリ **Settings → Secrets and variables → Actions → New repository secret** で `DISCORD_WEBHOOK_URL` を作成し、値にコピーしたWebhook URLを保存する。
4. 最初に **Actions → Test Discord notification → Run workflow** でDiscord接続だけを確認する。Discordに「✅ 大井競馬・自動予想通知：Discord接続テスト成功」が届けば成功。
5. 次に **Actions → Oi racing paper-pick monitor → Run workflow** でレース監視を手動実行する。大井開催がない時間帯や対象となる発走前の時間帯でなければ、通知なしは正常。
6. Actionsの定時起動が有効なら、大井開催日を自動で判定する（スケジュールは遅延・スキップの可能性あり）。

**料金注意：** GitHubの標準GitHub-hosted runnerはPublicリポジトリで無料です（公開されるのはコードと紙上予想履歴。Discord WebhookはSecretとして保持）。PrivateではGitHub Freeの月2,000分枠を超える可能性が高く、超過時の請求・停止は請求設定によります。必ずBillingのActions予算を確認してください。定期実行は全日12:00〜21:59 JSTに試行するため大井開催がない日にも実行します。

## デモテスト（通知は送らない）

Python 3.9以上（zoneinfo対応）が必要。付属テストファイルのZIPを読み、動作を確認できます。

```bash
LOCAL_RACE_ZIP=tests/sample_race.zip LOCAL_ODDS_ZIP=tests/sample_odds.zip python monitor.py --demo
python -m unittest discover -s tests -v
```

Windows PowerShellでは環境変数設定方法が違います。`--demo` であってもZIPを自動生成しないため、付属ファイルをそのまま使ってください。

## 現状の制約・次の改良

- 使用する過去成績は公式出馬表内の要約のみ。上がり、脚質、展開、当日馬場傾向等を学習していない
- 予想ロジックは**暫定試作**。必ず競走結果で的中率・回収率・改善の有無を検証してから実運用を判断する
- 単勝のみ対応。馬連・ワイド・3連複等は未実装
- 公式のタイムスタンプ付きZIPが得られない場合、本番は通知を停止
- CSV形式の更新、配信停止、GitHub Actionsの遅延で通知不能の場合がある
- `state.json` はGitHubのリポジトリ内で保存。Private推奨
- **勝馬投票券の自動購入はしません**
