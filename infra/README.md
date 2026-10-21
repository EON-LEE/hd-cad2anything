# 인프라

이 폴더의 Bicep은 PoC 전용 리소스 그룹 `rg-{environmentName}`을 만든다.

## 리소스

| 구분 | 리소스 | 비고 |
|---|---|---|
| AI | Foundry 계정(AIServices)·프로젝트, 모델 배포 | 주 모델 `gpt-6.1-sol`, 경량 `gpt-5.4-mini`, 임베딩 `text-embedding-3-small`(GlobalStandard) |
| 데이터 | PostgreSQL flexible server 16 + PostGIS | Microsoft Entra 인증만 사용(비밀번호 없음), Burstable B1ms |
| 저장소 | Storage 계정 | 컨테이너 `cad-revisions`, `artifacts`, `data`. 큐 `revision-events`. 공유 키 비활성 |
| 이벤트 | Event Grid 시스템 토픽 | `cad-revisions`에 `manifest.json`이 올라오면 큐에 메시지를 넣는다. |
| 컴퓨팅 | Container Apps 환경, Container Registry(Basic) | 서비스는 구현 단계에서 추가한다. |
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

## 주의

- Azure Maps는 한국에서 사용할 수 없으므로 쓰지 않는다.
- 모델 버전은 자동 업그레이드하지 않는다. 바꿀 때는 할당량을 확인한 뒤 매개변수로 지정한다.
- 비밀 값은 출력하거나 커밋하지 않는다. 앱은 관리 ID로 접근한다.
