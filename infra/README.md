# 인프라

이 폴더의 Bicep은 PoC 전용 리소스 그룹 `rg-{environmentName}`을 만든다.

## 리소스

| 구분 | 리소스 | 비고 |
|---|---|---|
| AI | Foundry 계정(AIServices)·프로젝트, 모델 배포 | 주 모델 `gpt-6.1-sol`, 경량 `gpt-5.4-mini`, 임베딩 `text-embedding-3-small`(GlobalStandard) |
| 데이터 | PostgreSQL flexible server 16 + PostGIS | Microsoft Entra 인증만 사용(비밀번호 없음), Burstable B1ms |
| 저장소 | Storage 계정 | 컨테이너 `cad-revisions`, `artifacts`, `data`. 큐 `revision-events`. 공유 키 비활성 |
| 이벤트 | Event Grid 시스템 토픽 | 구독은 아직 없다. 관리 ID 기반 큐 전달이 내부 오류로 실패해 보류했다. 개정 처리는 API로 실행한다. |
| 컴퓨팅 | Container Apps 환경, Container Registry(Basic) | 서비스는 `services.bicep`으로 따로 배포한다. |
| 운영 | Log Analytics, Application Insights, Key Vault(RBAC), 사용자 할당 관리 ID | — |

## 권한

| 대상 | 역할 |
|---|---|
| 개발자 | Foundry(Azure AI User, OpenAI User), Storage(Blob·Queue 데이터 기여자), ACR Push, Key Vault Secrets Officer, PostgreSQL Entra 관리자 |
| 앱 관리 ID | Foundry, Storage, ACR Pull, Key Vault Secrets User, PostgreSQL Entra 관리자 |
| Foundry 프로젝트 관리 ID | ACR Pull(hosted agent 이미지), Azure AI User |

## 배포

```powershell
$me = az ad signed-in-user show --query "{id:id, upn:userPrincipalName}" -o json | ConvertFrom-Json
az deployment sub create --location koreacentral --template-file infra/main.bicep `
  --parameters environmentName=cad2anything-kc principalId=$($me.id) principalName=$($me.upn) clientIpAddress=<개발 PC 공인 IP>
```

`azd`를 쓰는 경우 `AZURE_PRINCIPAL_NAME`과 `CLIENT_IP_ADDRESS`를 azd 환경 변수로 설정한 뒤 `azd provision`을 실행한다.

## 서비스 배포

기반 리소스를 만든 뒤 Atlas 서비스를 배포한다. Atlas는 컨테이너 앱 하나에서 웹, REST·채팅 API, MCP 엔드포인트(`/mcp`)를 함께 제공한다.

1. 작업공간을 만들어 Blob Storage의 `data` 컨테이너에 올린다. 앱은 시작할 때 이 컨테이너에서 작업공간을 내려받는다.

   ```powershell
   $env:DATA_STORAGE_ACCOUNT = '<storage 계정 이름>'
   uv run --no-sync python -m cad2anything.bootstrap --push
   ```

2. 이미지를 빌드해 Container Registry에 올린다.

   ```powershell
   az acr build --registry <registry 이름> --image atlas:<태그> --file services/atlas/Dockerfile .
   ```

3. 서비스를 배포한다. MCP 키는 배포할 때마다 새로 만들고 출력하지 않는다.

   ```powershell
   $key = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
   az deployment group create --resource-group rg-cad2anything-kc --template-file infra/services.bicep `
     --parameters environmentName=cad2anything-kc atlasImage=<registry>.azurecr.io/atlas:<태그> mcpApiKey=$key `
     --query properties.outputs
   ```

   이 배포는 다음을 만든다.
   - 컨테이너 앱 `ca-atlas-*`(복제본 1개 고정, 앱 관리 ID 사용)
   - Foundry 프로젝트 연결 `cad2anything-mcp`(`RemoteTool`, `x-api-key`). 에이전트는 이 연결로 MCP를 호출한다.

4. 출력값 `MCP_SERVER_URL`과 `MCP_CONNECTION_ID`를 환경 변수로 지정하고 에이전트를 배포한다.

   ```powershell
   uv run --no-sync python -m cad2anything.agents.deploy --dry-run
   uv run --no-sync python -m cad2anything.agents.deploy
   ```

## 주의

- Azure Maps는 한국에서 사용할 수 없으므로 쓰지 않는다.
- 모델 버전은 자동 업그레이드하지 않는다. 바꿀 때는 할당량을 확인한 뒤 매개변수로 지정한다.
- 비밀 값은 출력하거나 커밋하지 않는다. 앱은 관리 ID로 접근한다.
