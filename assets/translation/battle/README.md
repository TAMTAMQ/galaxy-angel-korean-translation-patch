# 전투 대화 번역 파일

번역할 파일은 `battle_unique.json`이다.

- `units[].translation`만 번역한다.
- `id`, `original`, `occurrences`는 수정하지 않는다.
- 원문의 줄 수와 마지막 `\n`을 유지한다.
- `state`는 번역 후 `draft`, 검토가 필요하면 `needs_human`으로 바꾼다.
- `use_translation`은 `true`로 유지한다.

현재 추출 결과:

- GADAT002 전투 스크립트: 163블록
- 실제 메시지 출현: 37,805건
- 중복 제거 번역 대상: 2,302건
- SLGINIT 런타임 복사본: 628블록 출현
- 런타임 복사본이 확인되지 않은 전투 스크립트: 0블록

`battle_units.json`은 모든 실제 출현 위치와 원본 바이트 오프셋을 담은 적용용
파일이다. `segments/`에는 같은 자료를 GADAT002 압축 블록별로 나눠 두었다.
번역문을 적용할 때는 중복 제거 번역을 모든 `occurrences`에 전파하고,
GADAT002와 각 `runtime_copies`의 SLGINIT 복사본을 함께 패치해야 한다.
