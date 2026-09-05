# 번역 작업 위치

실제로 번역할 파일은 `segments/*.json`이다. 파일 하나가 원본 시나리오 파일 하나에
대응하며, `index.json`에 빌드 대상 파일과 순서가 고정되어 있다.

번역 전에는 같은 디렉터리의 `glossary.tsv`와 `GLOSSARY.md`를 반드시 함께 제공한다.
인명·함선·문장기·세계관 용어는 `glossary.tsv`의 확정 표기를 사용하며 RA 시리즈
메카 명칭은 이 작품의 번역 범위에서 제외한다.
AI 대량 번역에는 `GALAXY_ANGEL_TRANSLATION_PROMPT.md`를 시스템 프롬프트 또는
최상위 작업 지시로 사용한다. 말투는 별도 캐릭터표를 만들지 않고 일본어 원문의
존댓말·반말을 그대로 따르며, 호칭은 `씨/군/쨩/님` 고정 대응을 적용한다.

각 `units` 항목에서 다음 세 필드만 편집한다.

```json
{
  "original": "원문(수정 금지)",
  "translation": "한국어 번역을 여기에 입력",
  "state": "draft",
  "use_translation": true
}
```

- `id`, `channel`, `context`, `original`은 수정하지 않는다.
- 줄바꿈은 JSON 문자열 안의 `\n`으로 유지한다.
- `⟦RAW:XX⟧` 토큰이 있으면 원문과 같은 순서로 번역에도 그대로 남긴다.
- 초안은 `draft`, 교차 검토 대기는 `review`, 판단이 필요하면 `needs_human`, 검수 완료는
  `complete`로 둔다.
- 번역을 게임 빌드에 적용하려면 `use_translation`을 `true`로 둔다. `false`이면 원문이
  유지되므로 기술 개발 빌드는 미번역 상태에서도 만들 수 있다.

검증:

```powershell
.\hanpatch\venv\Scripts\python.exe tools\galaxy_angel_translation.py validate `
  --source work\galaxy_angel\source\scenario `
  --assets work\galaxy_angel\assets\translation
```

재삽입 직전 시나리오 생성:

```powershell
.\hanpatch\venv\Scripts\python.exe tools\galaxy_angel_translation.py apply `
  --source work\galaxy_angel\source\scenario `
  --assets work\galaxy_angel\assets\translation `
  --output work\galaxy_angel\build\scenario
```

검증기는 원본 SHA-256, 안정 ID 모집단, 원문, 단위 수, 상태, RAW 토큰을 검사하고 하나라도
달라지면 실패한다. 현재 모집단은 대사 블록 18,165개이며 210개 JSON 조각으로 구성된다.

전체 패치 빌드:

```powershell
.\work\galaxy_angel\build_patch.ps1
```

번역에 사용된 한글은 빌드 때 자동으로 수집되어 실행 파일의 내장 24×24 글꼴에
배정된다. 별도의 인코딩 표를 손으로 관리할 필요가 없다. 현재 안전 한도는 고유 한글
2,045자이며 초과하면 빌드가 중단된다. 결과 ISO는
`work/galaxy_angel/build/Galaxy Angel (Korean).iso`에 생성된다.
