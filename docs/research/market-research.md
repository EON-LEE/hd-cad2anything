# Retail Spatial Intelligence · CAD2Anything 시장조사 보고서

- 조사 기준일: 2026-10-20
- 관점: 국내 대형 백화점 운영사. 층, zone, 매장, 매대, 동선 전체를 다룬다.
- 문서 성격: 공개 저장소용 일반화본이다. 특정 고객사 정보와 실제 고객 데이터를 포함하지 않는다.
- 조사 방법: 8개 트랙을 병렬로 조사했다.
  - 공간계획 벤더
  - 실내 동선 분석
  - CAD → 공간 데이터
  - AI Agent
  - 거래 × 공간 분석
  - Microsoft 스택
  - 도면 변경 재생성
  - 목데이터 출처
- 자료 우선순위: 제품 문서, 릴리스 노트, 고객 사례, 논문 같은 1차 자료를 먼저 사용했다.

## 읽는 법

| 표기 | 뜻 |
|---|---|
| [Doc] | 제품 문서, 데모, 논문 같은 1차 자료로 확인했다. |
| [Vendor] | 벤더의 마케팅 주장으로만 확인했다. |
| [3rd] | 언론, 분석가, 커뮤니티 자료로 확인했다. |
| [Inf] | 근거를 바탕으로 추론했다. |
| ✅ | 시장에서 확인된 기능이다. |
| 🆕 | 이 문서가 새로 제안하는 아이디어다. |

"찾지 못함"은 2026-10 기준 공개 1차 자료에서 확인하지 못했다는 뜻이다. 존재하지 않는다는 증명은 아니다.

---

## 0. 핵심 결론

### 0-1. 핵심 질문에 대한 답

> CAD와 거래 분석은 이미 존재한다. 자연어 에이전트가 분석뿐 아니라 Atlas의 node, edge, heatmap을 실시간으로 조작하고 what-if 공간 시뮬레이션까지 수행하는 경험은 얼마나 상용화되어 있는가?

| 단계 | 상용화 수준 | 근거 |
|---|---|---|
| (a) 자연어 → 공간 분석·질의 | 🟢 흔함 | CARTO, Esri, Databricks Genie, Snowflake Cortex Analyst, ThoughtSpot Spotter, Power BI Copilot이 제공한다. 국내 백화점의 자연어 BI 에이전트 운영 보도도 있다. |
| (b) 자연어 → 에이전트가 지도·평면도·그래프 화면을 그 자리에서 변경 | 🟡 일부 제품 | **지원:** CARTO의 지도 조작 tool API, ThoughtSpot agentic charts, kepler.gl AI Assistant, Felt AI, Power BI Copilot 보고서 편집, Kineviz Agent<br>**반대 사례:** Snowflake CoWork는 지도를 지원하지 않는다. Tableau Agent는 텍스트·표 중심이다. Placer.ai는 링크를 반환한다. |
| (c) 자연어 → what-if 공간 시뮬레이션 | 🟠 드묾 | **해당 사례:** CARTO의 상권 폐점 영향 모델링, Siemens Tecnomatix Copilot(공장)<br>**참고:** NVIDIA×Lowe's는 시뮬레이션을 하지만 자연어로 실행한다는 근거는 없다. |
| (d) 실내 CAD 평면도와 거래·고객 그래프 위에서 (a)+(b)+(c) 통합 | 🔴 찾지 못함 | 모든 조사 트랙이 각각 같은 결론에 도달했다. |

### 0-2. 추가로 확인된 사실

- CAD 도면 위에 매출·동선 heatmap을 표시하는 기능은 오래된 상용 카테고리다. ✅
- 매대 단위 planogram을 자연어로 생성·수정하는 기능은 2026년에 상용화되기 시작했다. 예: NIQ Optiq(2026-10-01), DotActiv Nova·Lola ✅
- 도면 개정을 시각적으로 비교하는 기능은 흔하다.
- 2D DWG/DXF 평면도의 의미 단위 변경 집합(매장 이동·확장·신설·삭제)을 공개 API로 제공하는 상용 제품은 찾지 못했다.
- 도면 변경 하나로 안내도, 면적표, 집기표, 분석이 동시에 재생성되는 사례는 찾지 못했다. 기존 동기화는 가져오기를 다시 실행하는 반자동 흐름이다.
- Microsoft의 CAD → 실내지도 서비스(Azure Maps Creator)는 2025-09-30 종료됐고 직접 후속 서비스가 없다. Azure Maps는 한국에서 사용할 수 없다. [Doc]
- LLM 단독의 실내 공간 추론은 신뢰하기 어렵다(FloorplanQA). 따라서 계산은 결정론적 도구가 맡아야 한다. [Doc]

### 0-3. 포지셔닝 🆕

이 제안은 **CAD2Anything 에이전트 플랫폼**이다.

- **공간관계 모델:** CAD를 공간관계 모델로 이해한다. 모델은 매장, 매대, 통로, 수직동선과 이들 사이의 포함, 인접, 연결 관계를 버전별로 관리한다.
- **변경 재생성 루프:** 도면이 바뀌면 에이전트가 변경을 해석하고 영향을 분석한다. 그 결과로 파생 정보와 애플리케이션을 재생성하고, 사람이 승인한 뒤 반영한다.
- **응용 분석:** 거래 분석과 what-if는 이 모델 위에서 동작하는 애플리케이션이다.
- **What-if 처리:** what-if는 "가상 도면 개정"으로 표현해 실제 개정과 같은 루프에서 처리한다.

---

## 1. 기존 솔루션과 경쟁 지형

### 1-1. 공간·플로어 플래닝과 공간 최적화

| 제품 | 핵심 기능 | CAD 지원 | POS 연계 | 시각화 | AI·GenAI·Agent | What-if·최적화 | 이 제안과의 차이 |
|---|---|---|---|---|---|---|---|
| StoreSpace (CADS) | DWG 기반 macro·micro space planning, NIQ Spaceman 연동 | DWG 네이티브 [Doc] | ✅ | 매출·동선·수익 heatmap | 찾지 못함 | model store 비교 | 에이전트, 그래프, 재생성 없음 |
| PlanoHero (Datawiz) | DXF 업로드, zone·fixture 배치, planogram | DXF [Doc] | ✅ | 시간대별 hot/cold zone | Wizora: 자연어 어시스턴트, 텍스트 답변 [Doc] | 규칙 기반 자동 배치 | 화면 직접 조작, 테넌트 what-if 없음 |
| SpaceSmart (Impact Analytics) | space elasticity, floor plan 최적화 | 불명확 | ✅ | 대시보드 | 자율 실행형 AI agent. 대화형 아님 | Affinity & Adjacency Intelligence [Doc] | 카테고리 단위다. 대화형 조작이 없다. |
| Blue Yonder | Space·Floor Planning, 3D | 약함 | ✅ | Performance Highlighting heatmap | Agentic AI(2026, 공급망 중심)<br>Shelf Ops Agent: 자연어로 planogram 일괄 편집 | scenario 언급 | 매장 전체나 테넌트 단위가 아니다. |
| RELEX | floor·macro space·planogram 통합 | 약함 | ✅ | floor plan color shading | AI agents 9종(2026-03). 공간 담당 agent 없음 [Doc] | 비공개 | 공간 에이전트 없음 |
| Oracle Retail | Macro Space Optimization, Affinity Analysis | 불명 | ✅ | affinity를 IF-THEN 규칙 표로 제공 [Doc] | 대화형 agent 찾지 못함 | space elasticity, what-if 시나리오 | 그래프, 자연어 없음 |
| NielsenIQ Spaceman·Optiq | planogram, macro·micro | StoreSpace 연동 시 DWG | ✅ | — | Optiq: 자연어로 planogram 생성·최적화(2026-10-01) [Doc] | 실행 gap 탐지 | 매대 단위다. 매장 배치가 아니다. |
| DotActiv | planogram, floor planning | 불명 | ✅ | ✅ | Nova: 자연어 → planogram<br>Lola: 자연어 → SQL [Doc] | 전략 기반 생성 | 매대 단위 |
| Quant | floor plan editor, 3D 트윈 | DWG 임포트 [Doc] | ✅ | 2D·3D | 찾지 못함 | 불명 | AI 없음 |
| Yardi Floorplan Manager | 임대·스태킹 플랜, 면적 계산 | 기존 AutoCAD 도면 활용 [Doc] | 임대 데이터 | 색상 코딩 | 찾지 못함 | 없음 | 분석, 에이전트 없음 |
| 자이로소프트 매장 공간관리(국내) | CAD 등록 → 구역·매장 생성 → ERP·매출 연동 → 버전 이력 → 개편 시뮬레이션 | CAD [Vendor] | ✅ | 불명 | 공개 근거 없음 | 개편 시뮬레이션 [Vendor] | 국내의 가장 가까운 사례다. 의미 단위 diff나 에이전트와 결합했다는 근거는 없다. |

### 1-2. 실내 동선·유동인구 분석과 실내지도

| 제품 | 데이터·센서 | CAD·도면 처리 | POS 연계 | GenAI·Agent | 이 제안과의 차이 |
|---|---|---|---|---|---|
| V-Count | 천장형 3D 센서 | 치수 도면을 배경으로 업로드 | API 연동 | AI Sales Coach(텍스트) | 그래프, 도면 조작 없음 |
| RetailNext | 카메라, Wi-Fi/BLE | floor plan heatmap | 전환율 중심 | Pulse AI: 자연어 Q&A, 텍스트 답변 [Doc] | 화면 재구성 없음 |
| Xovis | 3D 스테레오 센서 | 센서 좌표계 | BI 연동 | Agentic Layer: 센서를 MCP tool로 노출 [Doc] | 센서를 제어할 뿐, 도면 뷰를 조작하지 않는다. |
| Sensormatic ShopperTrak | AI 카메라 Re-ID | 불명 | 전환율 | 찾지 못함 | — |
| Cisco Spaces | Wi-Fi AP | CAD 임포트 [Vendor] | API | 찾지 못함 | POS, 그래프 없음 |
| Esri ArcGIS Indoors | IPS 애드온 | CAD·BIM·LiDAR 통합 [Doc] | 설계상 없음 | Indoors 전용 AI 찾지 못함 | 시설관리 중심 |
| Mappedin | 지도 데이터 | CAD 업로드, AI 보조 벡터화 [Vendor] | 없음 | 찾지 못함 | 길찾기 중심. 대형 몰 체인 사례가 있다. |
| Placer.ai | 모바일 패널 | 건물 단위 | 방문 중심 | MCP 기반 자연어 질의. 답변은 텍스트와 링크 [Doc] | 실내 zone 해상도가 아니다. |
| 국내 영상 분석 벤더(mAy-I 등) | 기존 CCTV | zone 단위 | 전환율 언급 | 찾지 못함 | 국내 주요 유통 그룹이 도입했다. 가명처리 특례를 활용한다고 명시한다. |
| 국내 실내지도 벤더(다비오 등) | 자체 매핑 | 국내 주요 백화점·몰 100여 곳의 실내지도를 구축하고 입퇴점을 반영한다 [3rd] | 없음 | 찾지 못함 | 지도 인프라다. 분석 기능은 별도다. |
| 통신사 유동인구(SKT, KT) | 기지국 | 해당 없음 | 상권 단위 카드매출 결합 | 찾지 못함 | 상권 단위이며 매장 내부를 다루지 않는다. |

### 1-3. 그래프·affinity·cross-shopping

| 구분 | 확인 내용 | 한계·의미 |
|---|---|---|
| Oracle Affinity Analysis | association rule을 IF-THEN 표로 제공한다. [Doc] | node-edge 그래프는 없다. |
| Placer.ai | cross-shopping과 co-tenancy를 자연어로 질의할 수 있다. 답변은 텍스트와 대시보드 링크다. [Doc] | 화면을 직접 바꾸지 않는다. |
| dunnhumby, Circana | basket affinity를 제공한다. | 그래프 UI는 확인하지 못했다. |
| Neo4j, TigerGraph | 범용 그래프 DB다. | 리테일 공간과 결합한 제품은 없다. |
| 연구 사례 | 쇼핑몰 평면도를 shop·corridor 그래프로 바꿔 복도 이용 확률을 예측한 HGNN(arXiv 2504.07645)<br>테넌트 믹스 배치 최적화(Omega, 2024) | 상용 제품은 아니다. |
| 컨설팅 맞춤 개발 제안 | 그래프 기반 테넌트 what-if를 맞춤 개발로 제안하는 사례가 있다. | 기성 제품이 없다는 간접 증거다. [Inf] |

### 1-4. 리테일 디지털 트윈

| 사례 | 내용 | 거래 연계 | What-if | 자연어 |
|---|---|---|---|---|
| NVIDIA Omniverse × Lowe's | 매장 3D 트윈, 함께 구매되는 상품 간 거리 heatmap, Bay Optimizer [Doc] | ✅ | ✅ planogram 대량 시뮬레이션 | 고객용 생성형 AI는 별도 축이다. 통합 근거는 없다. |
| Dassault 3DEXPERIENCE | virtual twin 개념 [Vendor] | 주장 | 주장 | 찾지 못함 |
| Matterport, NavVis | 3D 스캔 | 없음 | 없음 | 없음 |
| Azure Digital Twins | 범용 트윈 그래프. 2026-10 현재 서비스 유지 | 설계상 가능 | 기성 기능 없음 | 없음 |
| Fabric digital twin builder | 저코드 온톨로지, Preview [Doc] | 가능 | 시뮬레이션 엔진이 아님 | 공식 연동 찾지 못함 |

**시사점:** 디지털 트윈은 무겁고 비싸다. 이 제안은 3D 트윈 대신 경량 **공간관계 모델**을 쓴다.

### 1-5. 국내 백화점 시장 맥락

- **AI 도입 현황**
  - 롯데백화점은 자연어 질의 기반 AI BI 에이전트를 운영한다. 데이터 분석 시간이 70% 줄었다고 보도됐다(2026-07). [3rd]
  - 신세계는 서울대와 공동 연구한 AI 고객 분석 논문이 ICML 2026에 채택됐다. AI 세일즈 에이전트는 2027년 도입을 계획하고 있다. [3rd]
- **MD 개편 실무**
  - 연 2회 정기 개편(S/S, F/W)과 비정기 부분 개편이 있다. 성과에 따른 층·구역 이동이 일상적이다. [3rd]
  - 백화점 공식 협력사 안내에는 개편 후 지도와 DB를 수작업으로 반영한다고 나온다. [Doc]
- **핵심 KPI:** 평효율(매출 ÷ 영업면적), 객단가, 구매전환율, 교차구매율
- **층 전략 개념**
  - 분수효과: 지하·저층에서 모은 고객이 위층으로 이동한다.
  - 샤워효과: 상층에서 모은 고객이 아래층으로 이동한다.
  - 산수효과: 같은 층 안에서 고객이 돌아다닌다.
  - 세 개념 모두 일본 백화점 업계 용어에서 유래했다. 목적 구매가 늘면서 샤워효과가 약해진다는 지적도 있다. [3rd]
- **거래 구조:** 특약매입이 일반적이다. 판매 후 수수료를 공제해 정산하며, 수수료율은 개별 협상으로 정한다. [Doc]

---

## 2. CAD → 공간 데이터

### 2-1. 기술·제품

| 이름 | 유형 | 라이선스·비용 | 강점 | 한계 |
|---|---|---|---|---|
| ezdxf | Python DXF 라이브러리 | MIT | 순수 Python, GeoJSON 상호운용 | DWG를 직접 읽지 못함 |
| ODA File Converter | DWG↔DXF 변환기 | 무료판은 비상업 용도 | 사실상 표준 변환 경로 | 상용 사용 시 멤버십 필요 |
| LibreDWG | C 라이브러리 | GPLv3+ | 오픈소스 DWG 읽기 | copyleft |
| Aspose.CAD | 상용 SDK | 유료 | 다양한 포맷 | 비용 |
| Autodesk Platform Services | 클라우드 API | 종량제 | Model Derivative 속성 추출, Design Automation(헤드리스 AutoCAD) | 클라우드 종속 |
| GDAL/OGR DXF | 라이브러리 | MIT 계열 | GIS 포맷 변환 | 일부 엔티티 미지원 |
| FME | 상용 ETL | 비공개 가격 | CAD → IMDF 표준 경로 | 비용 |
| Esri ArcGIS Indoors | 실내 GIS | 엔터프라이즈 | Site → Facility → Level → Unit 모델 | 리테일 분석 기능이 아님 |
| Mappedin, Pointr | 실내지도 SaaS | 구독 | CAD 업로드와 AI 벡터화 | 검수는 수작업 |
| AxcelerateAI | 평면도 AI 서비스 | 서비스 | rooms·area·adjacency를 JSON으로 출력 [Vendor] | 부동산 평가 중심 |
| Azure Maps Creator | DWG 패키지 → 실내지도 | **2025-09-30 종료** | 레이어 규격 문서는 참고 자료로 유효 | 후속 서비스 없음 |
| Power BI Synoptic Panel, Shape Map | 배경 이미지와 영역 바인딩 | AppSource / Preview | 빠른 KPI 시각화 | 공간 연산 불가 |

관련 표준은 아래와 같다.

- IMDF: 2021년 OGC Community Standard로 채택됐다.
- IndoorGML 2.0 Part 1: 2025-06 발행됐다.
- IFC: `IfcSpace`, `IfcRelSpaceBoundary`로 공간과 경계를 표현한다.

### 2-2. 배경 이미지 vs 구조화 지오메트리

| 구분 | 배경 이미지와 영역 바인딩 | 구조화 지오메트리(폴리곤, 속성, 그래프) |
|---|---|---|
| 클릭 가능한 개체 | 구역을 수작업으로 그려야 가능 | 원본 좌표 그대로 가능 |
| 면적·인접 계산 | 불가. 외부에서 미리 계산해야 함 | 즉시 계산 |
| What-if | 사실상 불가 | 폴리곤만 바꾸면 다시 계산 |
| 도면 개정 시 유지보수 | 모든 작업을 수작업으로 반복 | 같은 파이프라인을 다시 실행 |
| 초기 구축 비용 | 낮음 | 높음 |
| 적합한 용도 | 경영진 KPI 대시보드 | 이 제안: 에이전트, 재생성, what-if |

### 2-3. 권장 파이프라인

1. DXF를 우선 입력으로 받는다.
2. 단위와 인코딩을 정규화한다(`$INSUNITS`, `$DWGCODEPAGE`, CP949).
3. 외부 참조(XREF)를 확인한다.
4. 레이어, 블록, 텍스트를 결정론적 규칙으로 먼저 분류한다.
5. 신뢰도가 낮은 항목만 LLM이 보조 해석한다. LLM은 근거와 신뢰도를 함께 반환한다.
6. 선분을 폴리곤으로 만든다(snapping 허용).
7. 라벨을 point-in-polygon으로 매칭한다.
8. 층을 정렬하고 수직동선을 연결한다.
9. 공간 피처를 계산한다. 예: 면적, 둘레, frontage, 인접, 통로망
10. 자동 QA를 실행한다.
11. 사람이 검토한다.

**원칙:** 기하 연산은 결정론적 코드가 맡는다. LLM은 이름과 의미 해석, 설명에만 쓴다.

### 2-4. 외부 도면 데이터 현실

- 공개 라이선스로 재사용할 수 있는 몰·백화점급 CAD는 사실상 없다.
- 연구 데이터셋(FloorPlanCAD, ArchCAD-400K, CubiCasa5k)과 국내 공공 도면 데이터는 주거 위주다. 일부는 비상업 라이선스나 이용약관 제약이 있다.
- 따라서 합성 DXF를 생성하는 것이 주 전략이다.
- Microsoft의 Azure Maps Creator 샘플 도면 패키지는 보조 검증용으로 쓴다.
  - 저장소에 CC0와 MIT 라이선스 파일이 함께 있어, 보수적으로 MIT 조건(저작권 고지 유지)을 따른다.
  - 패키지 1.0은 3개 층, unit 186개, DWG AutoCAD 2018 형식이다.

---

## 3. 거래 + 공간 분석

### 3-1. 분석 카탈로그

| 분석 | 질문 | 방법·지표 | 상용 현황 | PoC 적합성 |
|---|---|---|---|---|
| 평효율·sales density | 면적당 매출은 얼마인가? | 매출 ÷ 영업면적 | 업계 표준 | 높음 |
| GMROF | 마진까지 반영한 면적 생산성은? | 매출총이익 ÷ 면적 | 업계 표준 | 높음 |
| Space elasticity | 면적을 늘리면 매출이 얼마나 늘까? | ln(Sales) = α + β·ln(Space) | Oracle MSO 등 | 중간 |
| Basket·category affinity | 무엇을 함께 사는가? | support, confidence, lift | 표준 | 높음 |
| Cross-shopping | A 고객 중 B도 사는 비율은? | Jaccard, confidence | Placer.ai 등 | 높음 |
| 고객-브랜드 네트워크 | 어떤 군집이 있는가? | bipartite projection + Louvain | 학술·실무 | 중간 |
| 구매 시퀀스 | 반복되는 구매 순서가 있는가? | PrefixSpan, Markov 전이 | 학술·실무 | 중간 |
| 층간 이동 | 분수효과·샤워효과는 얼마나 큰가? | Markov 전이 행렬 | 실무 | 높음 |
| Adjacency 효과 | 붙어 있으면 매출이 오르는가? | 회귀. 이상적으로는 DiD | 학술 | 중간(상관 수준) |
| 노출도 | 구조적으로 좋은 자리는 어디인가? | 에스컬레이터 거리, space syntax | 학술 | 중간 |
| Huff형 흡인력 | 이 자리가 고객을 끌어들일 확률은? | P_ij ∝ A_j^α · D_ij^-β | 학술 | 높음(what-if 근사) |
| 배치 최적화 | 전체 최적 배치는 무엇인가? | QAP 휴리스틱 | 식품점 연구 사례 | 2단계 |
| 보행 시뮬레이션 | 동선 혼잡은 어떻게 바뀌는가? | social force(MassMotion 등) | 몰 프로젝트 사례 | 범위 밖 |

### 3-2. 결제 시퀀스 ≠ 물리적 동선

결제 데이터만으로는 아래 정보를 알 수 없다.

- 실제로 걸은 경로
- 구매 없이 들른 매장
- 체류 시간
- 동반자 구매 여부

반품은 구매 시점과 다른 때에 발생해 시퀀스를 왜곡한다. 실제 쇼핑 경로는 매대를 순서대로 훑지 않는다는 연구도 있다(Hui, Bradlow, Fader 2009). [Doc]

물리적 동선을 보려면 아래 센서 데이터가 추가로 필요하다.

| 기술 | 가능한 것 | 프라이버시 위험 |
|---|---|---|
| 도어 카운터, 열화상 | 출입 인원 수 | 낮음 |
| Wi-Fi probe | 방문·재방문 추정 | 중간. MAC 주소는 개인정보다. |
| BLE 비콘 + 앱 | 옵트인 고객 위치 | 중간 |
| 천장형 3D 센서 | 인원 수, 체류 시간, 궤적 | 중간 |
| 카메라 Re-ID | 구간 간 동일인 추적 | 높음 |
| LiDAR | 익명 3D 궤적 | 낮음 |
| 스마트카트 | 동선과 구매의 연결 | 중간 |
| 통신사 기지국 | 상권 단위 유동인구 | 낮음. 집계 데이터다. |

국내 규제에서 확인할 사항은 아래와 같다. [Doc]

- 개인정보 보호법 제25조: 고정형 영상정보처리기기
- 개인정보 보호법 제25조의2: 이동형 영상정보처리기기(2023 개정)
- 생체정보 보호 안내서(2024-12)
- 가명정보를 결합하려면 지정된 결합전문기관을 거쳐야 한다.

### 3-3. What-if 접근법

| 접근 | 장점 | 한계 | PoC 적용 |
|---|---|---|---|
| 투명한 점수(affinity × 거리 감쇠 × 면적 적합도) | 완전히 설명 가능하고 즉시 계산된다. | 가중치의 근거가 약하다. | ✅ 기본 |
| Huff형 흡인력 | 파라미터가 적고 설명 가능하다. | 몰 내부에 적용하면 외삽이다. | ✅ 함께 사용 |
| 회귀 기반 반사실 | 신뢰구간을 제시할 수 있다. | 재배치 사례 표본이 부족하다. | 2단계 |
| QAP 최적화 | 최적 배치의 틀을 제공한다. | NP-hard이고 비정형 제약이 많다. | 2단계 |
| 보행 ABM | 물리적으로 현실적이다. | 보정 데이터와 전문 인력이 필요하다. | 범위 밖 |

What-if 결과에는 반드시 "추정치" 라벨과 근거를 붙인다. 근거에는 데이터 기간, 가정, 추정 범위가 포함된다. 인접 효과는 인과가 아니라 상관이다. 에스컬레이터 근처라는 공통 원인처럼 다른 요인이 작용할 수 있다.

---

## 4. AI·Agent 기회

### 4-1. 화면을 조작하는 에이전트: 시장 증거

| 증거 | 제품·논문 | 출처 | 라벨 |
|---|---|---|---|
| 명명된 tool로 열린 지도를 조작한다(`add_layer`, `set_layer_style` 등). | CARTO AI Agents | https://docs.carto.com/carto-user-manual/ai-agents/ai-tools-reference.md | [Doc] |
| 후속 자연어 명령으로 기존 차트를 다시 렌더링한다. | ThoughtSpot | https://docs.thoughtspot.com/cloud/26.10.0.cl/charts-agentic-nl-editing | [Doc] |
| 명령 하나로 레이어를 만들고 스타일을 적용한다. | kepler.gl AI Assistant | https://raw.githubusercontent.com/keplergl/kepler.gl/master/docs/user-guides/ai-assistant.md | [Doc] |
| 같은 지도를 대화 턴마다 이어서 편집한다. | Felt AI | https://felt.com/blog/a-brand-new-era-of-gis | [Doc] |
| 기존 보고서의 시각화를 추가·변경·삭제하고 undo할 수 있다. | Power BI Copilot | https://learn.microsoft.com/power-bi/create-reports/copilot-create-reports | [Doc] |
| 라이브 그래프를 필터링하고 강조한다. | Kineviz Agent | https://kineviz.com/agent | [Vendor] |
| 지도 차트를 지원하지 않는다. | Snowflake CoWork | https://docs.snowflake.com/en/user-guide/snowflake-cortex/snowflake-cowork | [Doc] |
| 추천 후 사람이 "Create map"을 눌러야 한다. | Esri Business Analyst Assistant | https://doc.arcgis.com/en/business-analyst/web/use-business-analyst-assistant.htm | [Doc] |
| 폐점 영향을 모델링하고 이전 계획을 제안한다. | CARTO 리테일 AI Agents | https://carto.com/industries/retail/ai-agents/ | [Doc] |
| 자연어로 시뮬레이션 모델을 변경한다(공장). | Siemens Tecnomatix Copilot | https://blogs.sw.siemens.com/tecnomatix/simulation-without-limits-bringing-ai-digital-twin-and-copilots-to-the-factory-floor/ | [Doc] |
| LLM이 실내 배치를 일관되게 추론하지 못한다. | FloorplanQA (ICML 2026) | https://arxiv.org/abs/2507.07644 | [Doc] |
| 멀티 에이전트가 평면도를 자연어로 수정한다(주거 인테리어). | Intelligent Co-Design (ACADIA 2025) | https://arxiv.org/abs/2603.15341 | [Doc] |
| 지오스페이셜 에이전트 서베이에 리테일·실내 상업 범주가 없다. | Hashemi & Züfle 2026 | https://doi.org/10.20944/preprints202601.2236.v2 | [Doc] |

### 4-2. 화면 조작 표준 패턴

| 패턴 | 확인 내용 |
|---|---|
| AG-UI + CopilotKit | shared state, frontend tool, generative UI를 지원한다. Microsoft Agent Framework가 공식 지원한다. [Doc] |
| MCP Apps | 스펙이 클릭해서 지역을 드릴다운하는 인터랙티브 지도를 예시로 든다. [Doc] |
| A2UI | 이미 렌더링된 화면의 데이터만 갱신하는 바인딩 모델이다. [Doc] |

**설계 시사점:** 에이전트는 명명된 도구를 호출하고, 화면은 상태 diff로 갱신한다. LLM은 도구 선택과 설명을 맡고, 계산은 결정론적 엔진이 맡는다.

### 4-3. Agent 분류

| Agent | 질문 예시 | 도구·화면 동작 | 가치 | 난이도 | 시장 vs 신규 |
|---|---|---|---|---|---|
| Spatial Analyst | "면적 대비 매출 효율이 낮은 zone은?" | 공간 조인, 평효율 계산, heatmap, 강조, 카메라 포커스 | 저효율 공간을 즉시 식별 | 낮음~중간 | 개별 요소는 존재한다. 실내 CAD와의 결합은 🆕 |
| Customer Journey | "40대 VIP의 교차구매·층간 이동 패턴은?" | 그래프 질의, 코호트 필터, 도면 위 엣지 | 타깃 전략 수립 | 중간 | 평면도와 동기화된 그래프는 🆕 |
| Merchandising | "매출이 떨어진 매장의 원인은?" | 집기 단위 집계, 전후 비교 | 진열 교정 | 중간 | 매대 단위는 존재한다. |
| Layout Optimization | "인접했을 때 시너지가 큰 조합은?" | 인접 그래프 × 매출, 엣지 색상 | MD 개편 판단 | 높음 | 🆕 |
| What-if Simulation | "이 zone을 에스컬레이터 앞으로 옮기면?" | 가상 개정, 점수 모델, 비교 뷰 | 개편 리스크 정량화 | 높음 | 실내 리테일 적용은 🆕 |
| Executive Insight | "이번 분기 층별 요약과 리스크는?" | 자동 요약, 드릴다운 | 보고 시간 단축 | 낮음 | 흔함 |
| CAD Understanding·QA | "새 도면을 구조화하고 문제를 찾아줘" | 파싱, 의미 해석, QA | 전체 신뢰성의 전제 | 높음 | 🆕 |
| Leasing·Tenant Mix | "공실에 어떤 업종을 넣어야 시너지가 큰가?" | 임대 효율, 인접 affinity, 입점 what-if | 백화점의 핵심 수익 레버 | 중간~높음 | 🆕 |
| Promotion Placement | "팝업을 어디에 두면 노출이 가장 큰가?" | 시간대별 heatmap, 후보 랭킹 | 캠페인 ROI | 낮음~중간 | 일부 존재 |
| Anomaly Alert | "오늘 평소와 다른 zone은?" | 이상 탐지, 자동 강조 | 빠른 운영 대응 | 중간 | 공간 결합은 🆕 |

---

## 5. 확장 컨셉: 도면 변경 → 재생성 루프 🆕

### 5-1. 시장 성숙도

| 영역 | 등급 | 근거 |
|---|---|---|
| 시각적 도면 비교 | 🟢 흔함 | AutoCAD DWG Compare, BricsCAD, Bluebeam, Autodesk Construction Cloud, Procore |
| 개체 단위 의미 변경 집합 | 🟡 일부 | BIM 쪽은 성숙했다(Autodesk BIM Collaborate, Bentley iTwin Changed Elements API). 2D DWG/DXF 평면도용 공개 API는 찾지 못했다. |
| 개정 시 파생 산출물 자동 재생성 | 🟠 드묾 | IWMS(Archibus, Planon, FM:Systems), Yardi, Mappedin, ArcGIS Indoors는 재실행과 수작업 검수가 필요한 반자동 흐름이다. |
| LLM 에이전트의 변경 해석 + 영향 분석 + 사람 승인 | 🔴 상용 없음, 연구도 드묾 | CAD-Assistant, Text2BIM, CAD-Editor는 조작·생성 연구다. 개정 차이 해석과 승인 게이트를 통합한 사례는 없다. |
| 위 세 가지의 리테일·백화점 결합 | 🔴 찾지 못함 | 국내 매장 공간관리 시스템이 CAD↔ERP·매출 연동과 개편 시뮬레이션을 주장한다. 그러나 의미 단위 diff나 에이전트와 결합했다는 근거는 없다. |

### 5-2. 참조 아키텍처 패턴

1. **불변 개정과 changeset:** 도면을 저장할 때마다 새 버전으로 등록하고, 버전 쌍을 diff 입력으로 쓴다. Bentley iTwin diff job과 Speckle 버전 모델이 참조 사례다.
2. **이벤트 트리거:** "도면 개정 업로드" 이벤트가 파싱, diff, 영향 분석을 순서대로 실행한다. APS Webhooks와 Speckle Automate가 참조 사례다.
3. **멱등 재생성:** 생성 작업마다 고유 ID를 붙여 재시도해도 부작용이 없게 한다.
4. **위험도별 승인 게이트:** 면적표처럼 위험이 큰 산출물은 승인 후 반영한다. 안내도 라벨처럼 위험이 낮은 산출물은 사후 검토한다.
5. **경량 공간관계 모델:** IndoorGML Node-Relation Graph를 기본으로 한다. 셀은 노드, 인접·연결은 엣지다. Brick과 RealEstateCore식 관계 표현을 참고한다. IFC 전체 스펙보다 가볍다.

### 5-3. 재생성 대상 후보

| 우선순위 | 산출물 | 데모 가치 | 난이도 |
|---|---|---|---|
| 1 | 매대·집기 목록 | 높음 | 낮음 |
| 2 | 면적표(전용·공용·임대) | 높음 | 낮음~중간 |
| 3 | 층별 안내도·디렉토리 맵 | 매우 높음 | 중간 |
| 4 | 길찾기 그래프 | 중간~높음 | 중간 |
| 5 | POS-공간 매핑 + 공간 생산성 KPI | 높음 | 중간 |
| 6 | 사이니지 계획 | 중간 | 중간 |
| 7 | 피난 동선 규칙 점검(인증 아님) | 중간 | 중간~높음 |
| 8 | 청소·보안 순찰 동선 | 낮음~중간 | 낮음 |

---

## 6. Microsoft 기반 PoC 권고

### 6-1. 아키텍처

| 계층 | 구성 | Microsoft 서비스 | 상태 |
|---|---|---|---|
| 경험 | Atlas 웹앱: React, deck.gl OrthographicView, 그래프 뷰, CopilotKit | Azure Container Apps, Entra ID | GA |
| 에이전트 | Orchestrator와 전문 에이전트, workflow, 사람 승인(HITL) | Microsoft Agent Framework(Python), Foundry 모델·관측성 | GA. AG-UI 패키지는 prerelease |
| 도구 | MCP 서버: 공간, diff, 렌더·내보내기, 분석, 시뮬레이션, 생성기 레지스트리 | Azure Container Apps | GA |
| 데이터 | 공간관계 모델, 거래 데이터 | Azure Database for PostgreSQL + PostGIS | GA |
| 파일 | CAD 개정본, 산출물 | Blob Storage + Event Grid | GA |
| 확장 | 거버넌스, 경영진 뷰, 다른 클라우드의 데이터 | Fabric(OneLake S3 shortcut, Fabric IQ), Power BI | 일부 Preview |

다음은 피해야 한다.

- Azure Maps 의존: 한국에서 사용할 수 없다.
- Foundry의 Fabric data agent 연동: 2026-11-06 종료된다. Fabric IQ로 대체한다.
- Assistants API: 이미 종료됐다.
- Cosmos DB Gremlin: 신규 채택하지 않는다.
- Fabric SQL database: 공간 데이터 저장소로 쓰지 않는다.

### 6-2. Must-have / Nice-to-have

**Must-have**

- 합성 백화점 DXF(지하 1층~지상 6층)와 개정 3종: 기준, MD 개편, what-if
- CAD 이해 → 공간관계 모델: 버전 관리와 안정 ID 포함
- 개체 단위 diff → 영향 분석(생성기 의존성) → 재생성 → 승인
- 생성기 3종: 매대·면적표, 층별 안내도, POS 매핑·평효율
- 미리 정의하지 않은 산출물의 즉석 생성
- 공개 통계로 보정한 합성 거래·고객 데이터. 심은 패턴과 정답표 포함
- Atlas 웹앱에서 에이전트의 화면 조작: 강조, heatmap, 엣지, 포커스, 비교 뷰
- Azure 배포, 추적과 평가

**Nice-to-have**

- 길찾기 그래프와 경로, IMDF·GeoJSON 내보내기
- 피난 동선 규칙 점검, 팝업·사이니지 후보
- Teams·Microsoft 365 Copilot 채널, Fabric·Power BI 경영진 뷰
- DWG 입력(라이선스 확보 후), QAP 최적화, 센서 동선 레이어, 이상 알림

### 6-3. 데모 임팩트가 큰 3개 시나리오

| 시나리오 | 질문 예시 | 에이전트 흐름 | 화면 |
|---|---|---|---|
| ① MD 개편 도면 반영 | "S/S 개편 도면이 올라왔어. 뭐가 바뀌었고 무엇을 다시 만들어야 해?" | CAD 이해 → diff → 영향 분석 → 재생성 → 승인 | 개정 전후 오버레이, 변경 목록, 영향 그래프, 재생성된 산출물 |
| ② 공간 생산성과 고객 흐름 탐색 | "3층에서 면적 대비 효율이 낮은 zone과 원인은? 40대 VIP의 층간 이동은?" | Spatial Analyst + Customer Journey | 평효율 heatmap, 도면 위 엣지, 그래프, 근거 패널 |
| ③ What-if = 가상 개정 | "여성 컨템포러리 zone을 에스컬레이터 앞으로 옮기는 안을 비교해줘" | 가상 개정 생성 → 같은 루프 → 점수 모델 | 현재안과 시나리오 비교, 추정 범위, 주의 문구 |

**보조 장면:** 미리 정의하지 않은 산출물을 요청하면 에이전트가 도구를 조합해 즉석에서 생성한다. 이것으로 확장성을 보여준다.

### 6-4. 합성 데이터 스키마(요약)

| 영역 | 테이블 | 주요 필드 |
|---|---|---|
| 개정 | `revision` | `rev_id`, `parent_rev_id`, `kind`(actual, hypothetical), `source_file`, `created_at` |
| 공간 | `floor`, `zone`, `unit`, `fixture`, `opening`, `vertical_link`, `walkway_node`, `walkway_edge` | `entity_id`(안정 ID), `rev_id`, `geometry`, `area_m2`, `attributes` |
| 관계 | `spatial_relation` | `rev_id`, `from_id`, `to_id`, `type`(contains, adjacent_to, connected_to, faces, near), `metrics` |
| 변경 | `change_set`, `change_item` | `rev_from`, `rev_to`, `entity_id`, `change_type`(add, remove, move, resize, reassign), `details` |
| 업무 | `brand`(가상), `category`, `tenancy`, `pos_terminal` | `brand_id`, `category`, `unit_id`, `valid_from`, `valid_to`, `commission_rate` |
| 고객·거래 | `customer`, `transaction`, `transaction_line`, `calendar` | `pseudo_id`, `age_band`, `gender`, `tier`, `ts`, `pos_id`, `amount`, `items`, `is_return` |
| 선택 | `footfall_zone_hourly` | `zone_id`, `hour`, `count` |
| 검증 | `planted_effect` | `effect_id`, `description`, `parameters`, `expected_signal` |
| 산출물 | `generator`, `artifact`, `approval` | `inputs`, `outputs`, `dependencies`, `validators`, `status` |

### 6-5. 목데이터 확보 전략

**원칙**

- 모든 데이터는 저장소의 생성 스크립트와 고정 seed로 다시 만들 수 있어야 한다.
- 실제 개인정보와 실제 고객 데이터는 사용하지 않는다.
- 브랜드명은 가상으로 만든다.
- 공개 통계는 집계 분포를 보정하는 데에만 쓰고, 출처를 표시한다.

**보정용 공개 통계**

| 출처 | 쓰는 정보 | 용도 |
|---|---|---|
| 산업통상자원부 주요 유통업체 매출 동향(월간) | 백화점 상품군별 매출 증감 | 계절성, 카테고리 가중치 |
| KOSIS 소매판매액지수(백화점) | 월별 지수 | 월별 추세, 계절 계수 |
| 서울 열린데이터광장 상권분석서비스 추정매출(OA-22176) | 성별·연령·시간대·요일 매출 비중 | 고객 세그먼트와 시간 분포 |
| 서울 생활인구(OA-20314) | 시간대별 인구 | 시간대별 내점 강도 |
| DART 사업보고서 점포 현황 | 점포별 영업면적 | 면적과 층 구성 규모 |

**외부 데이터 사용 원칙**

| 데이터 | 사용 방식 | 이유 |
|---|---|---|
| UCI Online Retail II(CC BY 4.0), Ta Feng(CC0) | 구조 참고 | 재배포 가능 |
| dunnhumby, H&M(Kaggle 대회), AI Hub(비상업) | 원본 업로드 금지 | 라이선스 제약 |
| OpenStreetMap 실내 태깅 | 태깅 체계만 참고하고 좌표는 쓰지 않음 | ODbL share-alike 조건 |
| Apple IMDF | 스키마만 참고 | 샘플 재배포 제한 |

**규모(가정)**

- 가상의 플래그십 1개점으로 설정한다.
- 연매출 약 2조 원, 영업면적 약 85,000㎡, 객단가 약 14.5만 원
- 연간 거래 약 1,380만 건, 기간 24개월, MD 개편 2회
- 데모 DB는 분포를 유지한 채 절대량만 축소 계수로 줄인다.

**검증**

- 심은 패턴과 강도를 정답표(`planted_effects`)로 함께 기록한다.
- 에이전트가 이 패턴을 찾는지, 함정 패턴(공통 원인)에 속지 않는지 평가한다.

---

## 7. Capability 표

| Capability | Existing Market | Representative Products | Gap / Opportunity | PoC Priority |
|---|---|---|---|---|
| CAD 도면 기반 매장·카테고리 공간 시각화 | 흔함 | StoreSpace, PlanoHero, Quant, Yardi | 공간계획 도구 내부 기능이다. 분석·에이전트와 분리되어 있다. | 필수(기반) |
| CAD → 구조화 지오메트리 변환 | 도구는 흔함. 리테일 분석용은 드묾 | ezdxf, GDAL, FME, ArcGIS Indoors, Mappedin, Pointr | Microsoft 1st-party 후속 서비스가 없다(Creator 종료). | 필수 |
| 공간관계 모델(포함·인접·연결 그래프) | 일부(표준, 학술) | IndoorGML NRG, ArcGIS Indoors 모델, arXiv 2504.07645 | 리테일 운영 데이터와 결합한 실용 모델이 드물다. | 필수 |
| 도면 개정 → 의미 단위 변경 해석 | 2D 평면도는 찾지 못함 | (BIM) BIM Collaborate, iTwin Changed Elements | 2D 리테일 평면도용은 공백이다. | 필수(핵심) |
| 개정 → 다중 산출물 자동 재생성 + 승인 | 드묾 | Archibus, Planon, Mappedin(반자동) | 에이전트 기반 동시 재생성은 공백이다. | 필수(핵심) |
| 매출·동선 heatmap on floor plan | 흔함 | RELEX, StoreSpace, V-Count, RetailNext, Xovis | 상품화되어 차별점이 아니다. | 필수(기본기) |
| Affinity·cross-shopping 분석 | 흔함(표·규칙 형태) | Oracle Affinity Analysis, Placer.ai, Circana | 그래프와 평면도를 결합한 시각화가 없다. | 필수 |
| Adjacency를 고려한 공간 최적화 | 일부 | SpaceSmart, Oracle MSO | 카테고리 단위다. 테넌트 재배치는 아니다. | Nice(what-if와 결합) |
| 자연어 데이터 Q&A | 흔함 | Power BI Copilot, Genie, Cortex Analyst | 범용화되었다. | 필수(기본) |
| 자연어 → 화면 상태 조작 | 일부 | CARTO, ThoughtSpot, kepler.gl, Felt, Kineviz | 실내 CAD·그래프 결합이 없다. | 필수(차별) |
| 자연어 → what-if 공간 시뮬레이션 | 드묾 | CARTO(상권), Siemens(공장) | 실내 리테일·테넌트 단위는 없다. | 필수(차별) |
| 물리 동선 분석(센서) | 흔함 | Xovis, V-Count, Cisco Spaces, 국내 영상 분석 벤더 | 거래 데이터와는 별개 레이어다. | Nice(선택 레이어) |
| 3D 디지털 트윈 | 일부(대형 사례) | NVIDIA × Lowe's | 무겁고 자연어와 연동되지 않는다. | 범위 밖(경량 모델로 대체) |
| CAD 이해·데이터 품질 에이전트 | 찾지 못함 | ArcGIS Indoors 수작업 도구 | 시장 공백이며 신뢰성의 전제다. | 필수(루프 일부) |
| 테넌트 믹스·임대 에이전트 | 몰 내부용은 찾지 못함 | Placer.ai(상업용 부동산 일반) | 백화점 특화 기회다. | Nice |

---

## 8. 결론: 세 그룹

### 이미 시장에 흔한 것

- CAD·평면도 위 매출·동선 heatmap
- POS를 연계한 공간 KPI(평효율 등)
- 표·규칙 형태의 affinity와 cross-shopping
- 자연어 데이터 Q&A
- 센서 기반 동선 분석
- CAD 파싱 도구
- 도면 개정의 시각적 비교

### 일부 제품만 하는 것

- 자연어로 화면 상태를 바꾸는 기능. 실외 지도와 일반 차트에 한정된다.
- 매대 단위 자연어 planogram
- adjacency를 고려한 카테고리 공간 최적화
- CAD → 실내지도 반자동화
- BIM의 개체 단위 변경 집합
- 상권·공장 영역의 자연어 what-if

### 아직 차별화 가능성이 높은 것 🆕

1. 실내 CAD를 공간관계 모델로 이해하는 기능
2. 도면 개정 시 에이전트가 변경을 해석하고, 영향을 분석해 다중 산출물을 재생성한 뒤 승인받는 루프
3. 평면도와 동기화된 거래 그래프(node·edge)를 에이전트가 직접 조작하는 탐색 경험
4. 테넌트 단위 what-if를 가상 개정으로 실행하고 근거와 불확실성을 함께 제시하는 기능
5. 백화점 도메인 지식을 반영한 에이전트. 예: 특약매입, MD 개편, 분수·샤워효과

---

## 부록 A. 주요 출처

### 공간계획·분석 벤더

- StoreSpace: https://cadsonline.com/retail-space-planning/storespace/
- PlanoHero: https://planohero.com/en/retail-space-planning-software/
- PlanoHero Wizora: https://planohero.com/en/wizora/
- SpaceSmart: https://www.impactanalytics.ai/solutions/retail-space-planning
- RELEX floor planning: https://www.relexsolutions.com/solutions/retail-floor-planning-software/
- RELEX AI agents: https://www.relexsolutions.com/resources/introducing-relex-ai-agents-for-retail/
- Oracle Affinity Analysis: https://docs.oracle.com/en/industries/retail/ai-foundation-cloud-service/25.1.201.0/aifug/affinity-analysis1.htm
- NIQ Optiq: http://investors.nielseniq.com/news/news-details/2026/NIQ-Brings-New-AI-and-Automation-Capabilities-to-Retail-Space-Planning-and-Merchandising/default.aspx
- DotActiv: https://dotactiv.com/space-planning-automation-software
- Quant: https://www.quantretail.com/en/retail-floor-planning
- Yardi Floorplan Manager: https://www.yardi.com/product/floorplan-manager/
- Placer MCP: https://docs.placer.ai/page/placer-mcp
- 자이로소프트 매장 공간관리: https://gyrosoft.co.kr/space/store-space-management

### 실내 동선·실내지도·디지털 트윈

- V-Count: https://v-count.com/retail-heatmap-analytics-nano-prime/
- RetailNext Pulse AI: https://retailnext.net/platform/our-ai-advantage
- Xovis Agentic Layer: https://xovis-open-sdk.github.io/xovis-sdk/ai/agentic_layer/
- Esri ArcGIS Indoors: https://www.esri.com/en-us/arcgis/products/arcgis-indoors/overview
- ArcGIS Indoors CAD import: https://doc.esri.com/en/arcgis-pro/latest/help/data/indoors/import-cad-floor-plans-with-import-cad-to-indoor-dataset.html
- Mappedin Map Upload Portal: https://www.mappedin.com/resources/blog/feature-101-map-upload-portal/
- NVIDIA × Lowe's: https://blogs.nvidia.com/blog/lowes-retail-digital-twins-omniverse/
- Fabric digital twin builder: https://learn.microsoft.com/fabric/real-time-intelligence/digital-twin-builder/overview

### 도면 비교·동기화·공간 모델

- AutoCAD DWG Compare: https://help.autodesk.com/view/ACD/2026/ENU/?guid=GUID-2D69E78D-5C82-464F-B864-CD29D5720EB9
- Autodesk BIM Collaborate Compare: https://help.autodesk.com/cloudhelp/ENU/BIM360D-Design-Collaboration/files/About-Changes-in-Design/About-Change-Workflows/GUID-D4DF5D59-53EE-4C3A-A8AB-ADF85686BAD8.html
- Bentley iTwin Changed Elements API: https://developer.bentley.com/apis/changed-elements-v3/overview/
- APS Webhooks: https://aps.autodesk.com/en/docs/webhooks/v1/overview/
- Speckle Automate: https://docs.speckle.systems/developers/automate/introduction
- OGC IndoorGML: https://docs.ogc.org/is/14-005r3/14-005r3.html
- Brick Schema: https://brickschema.org/resources/
- RealEstateCore: https://doc.realestatecore.io/3.3/full.html
- CAD-Assistant: https://arxiv.org/abs/2412.13810
- Text2BIM: https://arxiv.org/abs/2408.08054
- CAD-Editor: https://arxiv.org/abs/2502.03997
- 신세계 협력사 MD 안내: https://www.shinsegae.com/company/partner/md.do

### CAD 처리·데이터

- ezdxf: https://pypi.org/project/ezdxf/
- ODA File Converter: https://www.opendesign.com/guestfiles/oda_file_converter
- LibreDWG: https://www.gnu.org/software/libredwg/
- GDAL DXF: https://gdal.org/en/stable/drivers/vector/dxf.html
- Azure Maps Creator 샘플 도면 패키지(CC0·MIT 병기, MIT 준수): https://github.com/Azure-Samples/am-creator-indoor-data-examples
- OSM Simple Indoor Tagging: https://wiki.openstreetmap.org/wiki/Simple_Indoor_Tagging
- 서울 상권분석서비스 추정매출: https://data.seoul.go.kr/dataList/OA-22176/S/1/datasetView.do
- 서울 생활인구: https://data.seoul.go.kr/dataList/OA-20314/S/1/datasetView.do
- KOSIS: https://kosis.kr
- Faker ko_KR: https://faker.readthedocs.io/en/master/locales/ko_KR.html
- IndoorGML 2.0 Part 1: https://docs.ogc.org/is/22-045r5/
- FloorPlanCAD: https://floorplancad.github.io
- ArchCAD-400K: https://arxiv.org/abs/2503.22346

### 분석 방법

- Huff(1963): https://www.jstor.org/stable/3144521
- Brueckner(1993): https://link.springer.com/content/pdf/10.1007/BF01096932.pdf
- Hui, Bradlow, Fader(2009): https://www.jstor.org/stable/10.1086/599046
- Ozgormus & Smith: https://www.eng.auburn.edu/~aesmith/files/a-data-driven-approach-to-grocery-store-block-layout.pdf
- Oasys MassMotion 몰 사례: https://www.oasys-software.com/case-study/shopping-malls-in-china-oasys-massmotion/
- PrefixSpan: http://hanj.cs.illinois.edu/pdf/span01.pdf

### Microsoft

- Azure Maps 지원 지역: https://learn.microsoft.com/azure/azure-maps/about-azure-maps
- Foundry Fabric 연동 종료: https://learn.microsoft.com/azure/foundry/agents/how-to/tools/fabric
- Fabric IQ tool: https://learn.microsoft.com/azure/foundry/agents/how-to/tools/fabric-iq
- Agent Framework AG-UI: https://learn.microsoft.com/agent-framework/integrations/by-component/ui/ag-ui/
- PostgreSQL 확장: https://learn.microsoft.com/azure/postgresql/extensions/concepts-extensions-versions
- OneLake shortcuts: https://learn.microsoft.com/fabric/onelake/onelake-shortcuts
- Power BI Shape Map: https://learn.microsoft.com/power-bi/visuals/power-bi-shape-map
- Fabric 지역 가용성: https://learn.microsoft.com/fabric/admin/region-availability

### 국내 규제

- 개인정보보호위원회 이동형 영상정보처리기기 안내서: https://www.pipc.go.kr/np/cop/bbs/selectBoardArticle.do?bbsId=BS217&nttId=10679
