# 詳細設計書：ブラウザLLMコーディングエージェント

## 1. ディレクトリ構造案
モジュール化と拡張性を考慮した構成です。

```text
browser-llm-agent/
├── extension/                # Lemurブラウザ用拡張機能
│   ├── manifest.json
│   ├── content/
│   │   ├── main.js           # コンテントスクリプトの司令塔
│   │   ├── optimizer.js      # 【負荷軽減】不要DOMの削除・リソースブロック
│   │   ├── observer.js       # 【監視】LLMの回答完了を検知・テキスト抽出
│   │   ├── injector.js       # 【注入】テキストボックスへの入力・送信ボタン押下
│   │   └── ui_helper.js      # 【UI】エージェントの状態を画面上に表示
│   ├── background/
│   │   ├── service_worker.js # バックグラウンド常駐処理
│   │   ├── server_client.js  # 【通信】Termuxサーバーとの通信管理
│   │   └── state_manager.js  # 【状態】実行ステータスや設定値の管理
│   └── options/
│       ├── options.html      # 設定画面（サーバーURL等）
│       └── options.js
│
└── server/                   # Termux上で動作するローカルサーバー
    ├── main.py               # サーバー起動・ルーティング
    ├── core/
    │   ├── receiver.py       # 拡張機能からのデータ受信・バリデーション
    │   ├── parser.py         # LLM出力からのコマンド・コード抽出（正規表現等）
    │   ├── executor.py       # コマンド実行（安全性のチェック含む）
    │   ├── file_ops.py       # ファイルの読み書き操作
    │   ├── recorder.py       # 実行履歴・ログの記録
    │   └── responder.py      # 実行結果の整形・拡張機能への返信
    ├── logs/                 # 実行ログの保存先
    └── workspace/            # エージェントが作業するディレクトリ
```

## 2. サーバー側コアモジュールの詳細

### core/receiver.py (出力受取モジュール)
- **役割**: 拡張機能から送られてくるHTTPリクエスト（またはWebSocketメッセージ）の窓口。
- **機能**:
    - 受信データの形式チェック。
    - 実行中フラグの管理（二重実行防止）。
    - 拡張機能へのクイックレスポンス（「受理しました」等の通知）。

### core/parser.py (実行内容読み取りモジュール)
- **役割**: LLMが生成した生のテキストから「何をすべきか」を抽出する。
- **機能**:
    - Markdownのコードブロック（` ```bash `, ` ```python ` 等）の抽出。
    - 特定の独自タグ（例: `<write path="file.txt">`）の解析。
    - 実行すべきコマンドと、書き込むべきファイル内容の分離。

### core/executor.py (実行モジュール)
- **役割**: 抽出されたコマンドをTermux環境で安全に実行する。
- **機能**:
    - 禁止コマンド（`rm -rf /` 等）のフィルタリング。
    - サブプロセスによるコマンド実行と標準出力・エラーのキャプチャ。
    - タイムアウト管理。

### core/file_ops.py (ファイル操作モジュール)
- **役割**: ワークスペース内のファイルに対する読み書き。
- **機能**:
    - 指定されたパスへの書き込み。
    - 既存ファイルの読み込み（LLMにコンテキストとして渡す用）。
    - ディレクトリ作成等のユーティリティ。

### core/recorder.py (記録・とりまとめモジュール)
- **役割**: 実行結果（標準出力、エラー、ファイル変更）を収集・整理し、送信用のレポートを作成する。
- **機能**:
    - `executor` や `file_ops` からのバラバラな実行結果を一つのレスポンスデータに統合。
    - 実行内容のログ保存（`logs/` への記録）。
    - LLMに伝えるべき情報の優先順位付け（長すぎる出力の省略など）。

### core/responder.py (返信・通信モジュール)
- **役割**: 記録係が作成したレポートを、拡張機能へ確実に届ける。
- **機能**:
    - 拡張機能側の `injector.js` が受け取れる形式（JSON等）でのパケット送出。
    - 通信プロトコル（HTTP/WebSocket）の管理。
    - 送信リトライや接続確認などの通信制御。

## 3. 拡張機能側モジュールの詳細

### content/optimizer.js (負荷軽減)
- **役割**: Android端末のリソース消費を抑える。
- **機能**:
    - 不要なサイドバー、広告、装飾用DOM要素の削除。
    - 重いアニメーションや不要なスクリプトの実行抑制。

### content/observer.js (監視)
- **役割**: LLMの回答状況をリアルタイムで監視する。
- **機能**:
    - 回答の生成開始と完了（ストリーミング終了）の検知。
    - 最新の回答テキストの抽出。

### content/injector.js (注入)
- **役割**: サーバーからのレスポンスをLLMにフィードバックする。
- **機能**:
    - テキストエリアへの文字列流し込み。
    - 送信ボタンの自動クリックエミュレーション。

### background/server_client.js (通信)
- **役割**: Termux上のローカルサーバーとの橋渡し。
- **機能**:
    - Fetch APIまたはWebSocketを用いたリクエスト送信。
    - タイムアウト処理と再試行ロジック。

### background/state_manager.js (状態管理)
- **役割**: 拡張機能の動作モードや設定を保持する。
- **機能**:
    - 自動実行のON/OFF状態の管理。
    - サーバーURLやAPIキー（必要な場合）の保存。

## 4. 処理フローの詳細
1. **[Extension: observer]** LLMの「回答完了」を検知。
2. **[Extension: server_client]** 抽出したテキストを `server/receiver.py` へ送信。
3. **[Server: receiver]** データを受信し、`parser.py` に渡す。
4. **[Server: parser]** 「コマンド」と「ファイル書き込み」を特定。
5. **[Server: executor/file_ops]** 処理を実行。
6. **[Server: recorder]** 実行結果を収集・要約し、ログに記録。
7. **[Server: responder]** 要約された結果を拡張機能へ返信。
8. **[Extension: injector]** 結果を受け取り、LLMの入力欄に貼り付けて自動送信。

## 5. MiniMax Agent (agent.minimax.io) API偵察結果

### 5.1 概要
2026-04-16時点の調査結果。minimax-free-api（LLM-Red-Team）のソースコード解析 + JSバンドル静的解析 + curl_cffi実験による。

### 5.2 ターゲット選定理由
- Claudeの出力で訓練した疑惑のある中国企業（MiniMax）のサービス
- Google連座BANリスクなし（OAuth認証だがMiniMax→Googleへの通報経路がない）
- 無料枠あり（1,200クレジット/日）、MiniMax-M2.7モデル利用可能

### 5.3 認証
- **ログイン**: Google OAuth（Continue with Google）のみ
- **トークン**: JWT形式、LocalStorageの`_token`キーに保存
  - payload: `{exp, user: {id: "realUserID", name, avatar, deviceID, isAnonymous}}`
  - 有効期限: 約40日
- **トークン取得**: ブラウザでログイン後、CDPまたはroot shell経由でChrome LevelDBからも抽出可能
  - `strings /data/data/com.android.chrome/app_chrome/Default/Local Storage/leveldb/*.ldb | grep eyJ`

### 5.4 APIエンドポイント
#### 旧API（/v1/api系 — hailuoai.com由来）
| エンドポイント | 用途 | 認証 | 状態 |
|---|---|---|---|
| `GET /v1/api/user/info` | ユーザー情報取得 | Token header | ✅ curl_cffiで動作確認 |
| `POST /v4/api/chat/msg` | チャット送信（SSE/H2） | Token header + Yy署名 | ❓ 旧ドメイン専用の可能性 |
| `POST /v1/api/user/device/register` | デバイス登録 | Token header | 未検証 |

#### 新API（/matrix/api/v1系 — agent.minimax.io）
| エンドポイント | 用途 | 認証 | 状態 |
|---|---|---|---|
| `POST /matrix/api/v1/chat/send_msg` | メッセージ送信 | x-signature + params | ❌ 401 |
| `POST /matrix/api/v1/chat/list_chat` | チャット一覧 | x-signature + params | ❌ 401 |
| `POST /matrix/api/v1/chat/get_chat_detail` | チャット詳細取得 | x-signature + params | 未検証 |
| `POST /matrix/api/v1/chat/stop_run_agent` | エージェント停止 | x-signature + params | 未検証 |
| `POST /matrix/api/v1/model/list` | モデル一覧 | x-signature + params | 未検証 |

#### send_msgリクエストbody
```json
{
  "msg_type": 1,
  "text": "メッセージ内容",
  "chat_type": 1,
  "attachments": [],
  "selected_mcp_tools": [],
  "backend_config": {},
  "sub_agent_ids": []
}
```

#### WebSocket（チャット用別経路）
- JSバンドル内にWebSocket経由の`sendMessage`も存在
- `{msg_type: 1, text: message, sender_id: user_id}`

### 5.5 セキュリティヘッダー（axiosインターセプターから抽出）
```
x-timestamp: Unix秒 (Math.floor(Date.now()/1000))
x-signature: MD5(`${timestamp}I*7Cf%WZ#S&%1RlZJ&C2${JSON.stringify(body)}`)
yy:          MD5(encodeURIComponent(url_with_params) + "_" + body_json + MD5(unix_ms) + "ooui")
Token:       JWT（ヘッダー）
```

クエリパラメータ（`i.mC`関数出力）:
```
device_platform=web, app_id=3001, biz_id=3001,
unix=ミリ秒, timezone_offset=32400,
uuid=UUID, device_id=JWT内deviceID, user_id=JWT内realUserID,
token=JWT, client=web, sys_language=en, lang=en
```

### 5.6 防御・障壁
1. **Cloudflare**: `agent.minimax.io`はCloudflareで保護。`curl_cffi` (impersonate="chrome")で突破可能
2. **署名検証**: x-signatureがMD5で検証されている（デタラメ→400、正しいMD5→401）
3. **地域制限の疑い**: `/v1/api`系は日本から通るが`/matrix/api`系は401。ソースにregion-restriction.htmlへの転送コードあり
4. **ドメイン分離**: 
   - `agent.minimax.io` = 国際版
   - `agent.minimaxi.com` = 中国版（別トークン必要、401）
   - `matrix-overseas-test.xaminim.com` = テスト環境

### 5.7 未解決・次のアクション
- [ ] `/matrix/api`の401原因特定（地域制限 vs 認証パラメータ不足）
  - Pixel 5からVPN(Proton)経由で検証予定
  - Playwright headed browserでの検証も有効
- [ ] WebSocket接続の解析（チャットの実態がHTTPかWSか）
- [ ] ブラウザ拡張方式のフォールバック実装（DOM操作でチャット）
- [ ] 署名のyy計算の正確な再現検証

### 5.8 参考リソース
- minimax-free-api (旧API): https://github.com/LLM-Red-Team/minimax-free-api
- MiniMax-Free-API fork (新API対応): https://github.com/xiaoY233/MiniMax-Free-API
- JSバンドル (API定義): `3566-69f731d0257a0e10.js` (146 API refs)
- JSバンドル (認証): `8081-fb87ca5745c42c05.js` (axios interceptor)
- JSバンドル (MD5): `9305-aa616530aaec57dd.js` (module 96467)
