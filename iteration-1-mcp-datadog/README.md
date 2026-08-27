# Iteration 1 (Datadog MCP variant): AgentCoreエージェントからDatadog MCP ServerをSAT認証で利用する

これは [iteration-1](../iteration-1/) をベースに、**エージェントがDatadogを「観測される側」としてだけでなく、Datadog MCP Server経由で「使う側」(ツール呼び出し先)にもする**構成を追加した検証用ディレクトリです。これまでのイテレーションはすべて「エージェントをDatadogで計装する」話でしたが、このディレクトリは「エージェント自身がDatadogのモニター・インシデント・ログなどをMCP経由で照会できるようにする」話です。専用のエージェント名(`agent_1_mcp_datadog`)を使用し、`iteration-1`本体のデプロイ済みエージェントには一切影響しません。

## Overview

- 実証するDatadog機能: Datadog MCP Server(Model Context Protocol)への接続、Service Account Token(SAT)によるBearer認証
- 技術スタック: Amazon Bedrock AgentCore Runtime上のLangGraphエージェント(Python)、[`langchain-mcp-adapters`](https://pypi.org/project/langchain-mcp-adapters/)によるMCPクライアント、Amazon API Gateway、AWS WAF、Amazon Cognito(OAuth、iteration-1と同じ)
- 既存のAPM + LLM Observability計装(`ddtrace` + `LLMObs.enable(...)`)はiteration-1のまま維持しています — このバリアントはそれに加えて、エージェントの**ツールとして**Datadog MCPを追加するものです。

## Architecture

```mermaid
flowchart LR
    Client(["🖥️ Client<br/>(ブラウザ)"])
    WAF["AWS WAF"]
    APIGW["Amazon API Gateway"]

    subgraph Runtime["Amazon Bedrock AgentCore Runtime"]
        AgentPy["agent.py<br/>(LangGraph ReAct agent)"]
    end

    Weather["api.weather.gov<br/>(既存ツール: get_weather)"]
    MCP["🐶 Datadog MCP Server<br/>https://mcp.&lt;DD_SITE&gt;/api/unstable/mcp-server/mcp<br/>設定箇所: agent/agent.py の MultiServerMCPClient +<br/>環境変数 DD_MCP_TOKEN (SAT) / DD_MCP_TOOLSETS"]

    Client --> WAF --> APIGW --> Runtime
    AgentPy -- "ツール呼び出し" --> Weather
    AgentPy -- "ツール呼び出し(Bearer SAT)" --> MCP

    style MCP fill:#632CA6,stroke:#632CA6,color:#fff
```

## Datadog設定

- 有効化している機能: Datadog MCP Server接続(エージェントのツールとして)。あわせて、iteration-1から引き継いだAPM + LLM Observability(`ddtrace`)も有効。
- 関連ファイル:
  - `agent/agent.py` — `_load_datadog_mcp_tools()` が `langchain_mcp_adapters.client.MultiServerMCPClient` でDatadog MCP Serverに接続し、取得したツールを既存のローカルツール(`get_current_time`、`get_weather`)と一緒にLangGraphエージェントへ渡します。
  - `agent/requirements.txt` — `langchain-mcp-adapters` を追加。
- 接続先エンドポイント: `https://mcp.<DD_SITE>/api/unstable/mcp-server/mcp?toolsets=<DD_MCP_TOOLSETS>`(`DD_SITE`が`datadoghq.com`なら`https://mcp.datadoghq.com/...`。実際に`curl`でこのURLへの未認証アクセスが`401 Unauthorized`を返すことを確認済みで、エンドポイント自体は実在します)。
- 認証方式: **Service Account Token(SAT)**を`Authorization: Bearer <SAT>`ヘッダーで送信。Datadogの通常のAPIキー/アプリケーションキー(`DD_API_KEY`/`DD_APPLICATION_KEY`)ではこのエンドポイントは認証できません(`Authorization: Bearer`にAPIキーを入れて試したところ`401`)。SATはサービスアカウント単位で発行する専用のトークンです。
- 必要な環境変数 / APIキー(`agentcore deploy`実行時):
  ```bash
  agentcore deploy \
    --env "DD_API_KEY=${DD_API_KEY}" \
    --env "DD_SITE=datadoghq.com" \
    --env "DD_LLMOBS_ML_APP_NAME=agentcore-iteration-1-mcp-datadog-agent" \
    --env "DD_ENV=sandbox" \
    --env "DD_SERVICE=agentcore-iteration-1-mcp-datadog-agent" \
    --env "DD_TRACE_LANGCHAIN_ENABLED=false" \
    --env "DD_MCP_TOKEN=${DD_MCP_TOKEN}" \
    --env "DD_MCP_TOOLSETS=all"
  ```
  | 環境変数 | 用途 |
  |---|---|
  | `DD_MCP_TOKEN` | Datadog Service Account Token(SAT)。MCP ServerへのBearer認証に使用 |
  | `DD_MCP_TOOLSETS` | 公開するtoolsetの範囲。省略時は`all`。SATの最小権限の原則に合わせて`apm,llmobs`のように絞り込むことも可能 |
  | `DD_SITE` | MCP ServerのURL(`https://mcp.<DD_SITE>/...`)の組み立てにも使用(既存のDatadog計装と共通) |

  `DD_MCP_TOKEN`が未設定の場合、エージェントはDatadog MCPのツールなしで(既存のローカルツールのみで)動作します — 設定を段階的に試せるようにするためのフォールバックです。

### SAT(Service Account Token)の作成方法

1. Datadogの **Organization Settings → Service Accounts** で対象のサービスアカウントを開く(なければ新規作成)
2. Access Tokensセクションの **+ New Token** をクリック
3. トークン名・有効期限(1日/1ヶ月/1年/なし/カスタム)を設定
4. **スコープを必要最小限に絞って**選択(このエージェントに使わせたいtoolsetに対応する権限のみ)
5. 保存し、表示されたトークン文字列を安全な場所に控える(再表示不可)

サービスアカウント自身への`service_account_write`権限、または組織全体に対する`org_app_keys_write`権限が必要です。詳細は[Service Access Tokensのドキュメント](https://docs.datadoghq.com/account_management/service-access-tokens/)を参照してください。

## Prerequisites

iteration-1と同じ共有 `agentcore-cognito` スタックとテストユーザー — 手順は[iteration-1](../iteration-1/README.md)のREADMEを参照してください。加えて:

- 上記の手順で発行したDatadog SAT(`DD_MCP_TOKEN`)

## Setup / How to Run

1. **前提条件**: iteration-1と同じ(共有 `agentcore-cognito` スタック、テストユーザー)。
2. **エージェントの設定・デプロイ** — iteration-1と同じ `agentcore configure` フロー(`direct_code_deploy`、`PYTHON_3_11`、同じCognitoプールを指すOAuth authorizer)ですが、既にデプロイ済みの `agent_1` と衝突しないよう、別名の `agent_1_mcp_datadog` を使用します。
3. **デプロイ**: 上記のDatadog設定セクションの環境変数を指定して `agentcore deploy` を実行します。
4. **API Gateway + フロントエンド**: iteration-1と同一 — 新しいエージェントのRuntime IDで `api-gateway.yaml` をデプロイし、`frontend/index.html` の `CONFIG` を更新します。
5. **テスト**: エージェントを呼び出します(`agentcore invoke --bearer-token <cognitoのIDトークン>` またはフロントエンド経由)。例: `{"prompt": "Datadogで現在アラート状態のモニターを教えて"}` のようなプロンプトで、Datadog MCP経由のツール呼び出しが行われるか確認します。

## Verify in Datadog

- **LLM Observability / APMトレース** — `service:agentcore-iteration-1-mcp-datadog-agent` で検索し、通常のLangGraphのLLM呼び出しスパンに加えて、Datadog MCPのツール呼び出しに対応するスパンが記録されていることを確認します。
- **応答内容** — プロンプトに対するエージェントの回答に、Datadog MCP経由で取得した実データ(モニターの状態、インシデント名など)が反映されていることを確認します。
- **認証エラーの見分け方** — `DD_MCP_TOKEN`が無効/期限切れの場合、CloudWatchログに`ExceptionGroup`にラップされた`httpx.HTTPStatusError: Client error '401 Unauthorized' for url 'https://mcp.<DD_SITE>/api/unstable/mcp-server/mcp?toolsets=...'`が出力されます(実際にダミートークンで確認済み)。この場合エージェントはDatadog MCPのツールなしで動作を続けます(次項「留意点」参照)。

## Cleanup

```bash
aws cloudformation delete-stack --stack-name agentcore-api   # このために API Gateway をデプロイした場合
cd agent && agentcore destroy
# 他のイテレーションが使っている共有Cognitoスタックは削除しないこと
```

## Notes

- **フォールバック動作**: `_load_datadog_mcp_tools()`は`DD_MCP_TOKEN`が未設定の場合、例外を投げずに空リストを返します。これは「まずMCP連携なしでエージェントの基本動作を確認し、その後SATを設定してMCPツールを有効化する」という段階的な検証をしやすくするための挙動です。ただし認証エラー(トークンが設定されているが無効)の場合は`asyncio.run(_load_datadog_mcp_tools())`が例外を伝播させ、`get_agent()`の初期化自体が失敗します — 「未設定」と「無効なトークン」は挙動が異なる点に注意してください。
- **接続はツール呼び出しごとに再確立されます**: `langchain-mcp-adapters`の`MultiServerMCPClient.get_tools()`は「A new session will be created for each tool call」という仕様です(ライブラリのdocstringより)。つまりエージェント起動時に一度`get_tools()`でツール一覧を取得した後、実際のツール呼び出し(HTTPリクエスト)はLangGraphがそのツールを実行するたびに新しいMCPセッションを張り直します。ロングランのMCPセッションを維持する設計ではありません。
- **`?toolsets=all`のセキュリティ上の注意**: 本ディレクトリのデフォルトはDatadogの全機能(モニター作成・ダッシュボード変更なども含む)にエージェントがアクセスできる設定です。実運用では、SAT自体のスコープと`DD_MCP_TOOLSETS`(例: `apm,llmobs`)の両方を、エージェントに実際に必要な範囲まで絞り込むことを推奨します。
- このディレクトリは`iteration-1`のOAuthパススルー構成をベースにしていますが、Datadog MCP接続の仕組み自体は`iteration-2`/`iteration-3`(IAM認証、Lambda経由)の構成にも同様に追加できます。
