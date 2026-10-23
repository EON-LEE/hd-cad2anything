# 업로드 예시 파일

Atlas의 도면·메타데이터 업로드를 시험하는 합성 예시 파일이다. 실제 고객 도면이 아니다.

| 파일 | 내용 |
|---|---|
| `R3_FW2026_4F-6F.zip` | 2026 F/W 개편안(`FW2026`의 ALT-2)을 CAD팀이 그렸다고 가정한 4·5·6층 도면 3개(`R3_4F.dxf` 등). 기본 규약(`default` 프로필)을 따른다. 계획과 다른 점 두 곳을 일부러 넣었다: Novier & Co를 계획한 4F-C20 대신 옆 매장 4F-C21에 그렸고, 6F-E01을 약 5% 작게 그렸다. |
| `customer_style_6F.zip` | 가상 플래그십점 R2 개정의 6층을 고객사 스타일 규약으로 다시 그린 `6F.dxf` 1개. 도면 헤더에 단위가 없고 좌표는 mm다. 레이어 이름은 한글(`매장_경계`, `매장_명칭` 등)이다. 매장 번호는 `6층 C-05`이고 브랜드명은 다음 줄에 쓴다. 존은 `1구역 스포츠`, 수직동선은 `E/S-A`·`E/V-1`·`계단실-1`로 쓴다. |
| `profiles/customer_style.yaml` | 고객사 스타일 규약을 Atlas 표준 코드(`6F-C05`, `6F-Z1`, `ESC-A`)로 바꾸는 도면 규약 프로필이다. 형식은 `docs/specs/drawing-profile.md`를 본다. |

계약 정보와 개편 브리프 예시는 `data/samples/metadata/`에 있다.

## 시나리오 1: 개편안대로 그린 도면 올리기

전제: 계약 정보(`leases.csv`)와 브리프(`FW2026.yaml`)가 올라가 있고, 레이아웃 탭의 시즌 개편에서 `FW2026` 대안을 만든 뒤 **ALT-2를 선택**했다.

1. 개정 검토 탭의 도면 업로드에서 `R3_FW2026_4F-6F.zip`을 고른다.
2. 개정 ID `R3`, 부모 개정 `R2`, 적용일 `2026-10-01`(브리프 적용일), 프로필 `default`를 넣는다.
3. 작업 상태에서 결과를 본다.
   - 변경 비교: 이동·교체·면적 변경 12건
   - 계약 정보 대조: 4F-C21 브랜드 불일치, 6F-E01·6F-C14 면적 불일치
   - 개편안 대조: 계획한 변경 12건 중 11건 일치(일치율 92%), 계획에 없는 변경 1건, 면적 변화 1건

적용일이 브리프 적용일과 같고 부모가 브리프의 기준 개정이어야 개편안 대조가 자동으로 실행된다. 다른 대안을 선택했다면 일치율이 낮게 나온다.

## 시나리오 2: 고객사 형식 도면을 프로필로 읽기

1. 메타데이터 업로드에서 종류 `drawing_profile`을 고르고 `profiles/customer_style.yaml`을 올린다.
2. 도면 업로드에서 `customer_style_6F.zip`을 고른다. 개정 ID `R2-CS`, 부모 개정 `R2`, 적용일 `2026-11-01`, 프로필 `customer_style`을 넣는다.
3. 작업 상태에서 단계별 결과를 본다. 6층만 올렸으므로 나머지 층은 R2에서 그대로 가져온다. 6층 배치는 R2와 같으므로 변경은 없어야 한다.

프로필을 `default`로 두고 같은 파일을 올리면 매장 번호를 읽지 못해 작업이 실패한다. 프로필이 필요한 이유를 보여 준다. 이 개정은 시연용 사본이므로 데모가 끝나면 데모 런북의 초기화 절차로 지운다.

API로 올릴 때의 예다.

```bash
curl -F kind=drawing_profile -F file=@data/samples/uploads/profiles/customer_style.yaml http://localhost:8000/api/uploads/metadata
curl -F files=@data/samples/uploads/customer_style_6F.zip -F rev_id=R2-CS -F parent_rev=R2 \
     -F effective_date=2026-11-01 -F profile=customer_style http://localhost:8000/api/uploads/drawings
```

## 다시 만들기

```bash
uv run --no-sync python -m cad2anything.mockgen.upload_samples --root data/workspace --out data/samples/uploads
uv run --no-sync python -m cad2anything.mockgen.relayout --root data/workspace --r3-sample data/samples/uploads
```

두 번째 명령은 `FW2026` 대안을 브리프 기본값으로 다시 계산하고, 순효과가 가장 큰 대안으로 R3 예시를 만든다.
