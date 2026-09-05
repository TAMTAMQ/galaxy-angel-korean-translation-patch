# 추가 추출 텍스트 번역 상태

2026-08-24 기준으로 새로 추출한 일본어 문자열의 번역문 입력을 완료했다.

## 번역 완료 목록

- `../battle/battle_unique.json`: 전투 대사 고유 문자열 2,302개
- `remaining_compressed_unique.json`: 기타 압축 데이터 문자열 859개
- `remaining_direct_review.json`: 실행 파일/비압축 검토 문자열 1,070개

번역에는 로컬 `gemma-4-26b-a4b-it-qat`를 사용했다. 고유 번역 결과는
`galaxy_angel_sync_extracted_translations.py`를 통해 전투 대사 occurrence 파일과
`by_container/` 파일에도 동기화했다.

## 검수 결과

- 검사한 JSON: 170개
- 검사한 단위: 80,700개(고유 항목과 동기화된 occurrence 포함)
- 빈 번역: 0개
- 번역문 내 일본어 잔존: 0개
- 마지막 개행 불일치: 0개
- printf/RAW 자리표시자 불일치: 0개
- `translation_error`: 0개

`remaining_direct_review.json`은 화면용 문자열 외에 개발 로그, 문자표, 오검출
바이트열이 섞여 있다. 번역문은 모두 채웠지만 안전 검토 전에는 패치하지 않도록
1,070개 모두 `use_translation: false`로 유지했다.
