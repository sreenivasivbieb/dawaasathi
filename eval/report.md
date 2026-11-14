# Dawaa Saathi – drug-entity eval (text mode)

Generated 2026-11-13T01:04:08 · 400 items from 100 synthetic utterances · DB: 39 brands (DEV FIXTURE: approximate MRPs, not verified - replace with your brand DB) · thresholds T1=0.8 T2=0.06

> Text mode: transcripts are the generated utterance text (optionally corrupted), not Prisma output. Use --audio for real STT accuracy by SNR and sample rate.

**Entity** = brand family and strength both right · **Brand** = family only · **SKU** = exact product · **Auto-accept** = matcher confident at T1/T2 (no confirmation question) · **Precision** = entity-correct among auto-accepted.

## Overall

| set | n | Top-1 entity | Top-3 entity | Top-1 brand | Top-3 brand | SKU | Auto-accept | Precision |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| all | 400 | 98.0% | 100.0% | 100.0% | 100.0% | 96.8% | 76.5% | 98.7% |

## By simulated ASR error rate (per-token corruption probability)

| asr_noise | n | Top-1 entity | Top-3 entity | Top-1 brand | Top-3 brand | SKU | Auto-accept | Precision |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 100 | 98.0% | 100.0% | 100.0% | 100.0% | 97.0% | 78.0% | 98.7% |
| 0.1 | 100 | 98.0% | 100.0% | 100.0% | 100.0% | 97.0% | 77.0% | 98.7% |
| 0.2 | 100 | 98.0% | 100.0% | 100.0% | 100.0% | 97.0% | 73.0% | 98.6% |
| 0.3 | 100 | 98.0% | 100.0% | 100.0% | 100.0% | 96.0% | 78.0% | 98.7% |

## By number style (clean text)

| number_style | n | Top-1 entity | Top-3 entity | Top-1 brand | Top-3 brand | SKU | Auto-accept | Precision |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| digits | 35 | 97.1% | 100.0% | 100.0% | 100.0% | 94.3% | 85.7% | 100.0% |
| hi_words | 25 | 96.0% | 100.0% | 100.0% | 100.0% | 96.0% | 80.0% | 95.0% |
| en_words | 20 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 95.0% | 100.0% |
| none | 18 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 38.9% | 100.0% |
| en_colloquial | 2 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |

## By brand script (clean text)

| brand_script | n | Top-1 entity | Top-3 entity | Top-1 brand | Top-3 brand | SKU | Auto-accept | Precision |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| latin | 65 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 78.5% | 100.0% |
| dev | 35 | 94.3% | 100.0% | 100.0% | 100.0% | 91.4% | 77.1% | 96.3% |

## By carrier phrase language (clean text)

| carrier | n | Top-1 entity | Top-3 entity | Top-1 brand | Top-3 brand | SKU | Auto-accept | Precision |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| dev | 59 | 96.6% | 100.0% | 100.0% | 100.0% | 94.9% | 76.3% | 97.8% |
| roman | 16 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 81.2% | 100.0% |
| en | 25 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 80.0% | 100.0% |

## By template (clean text)

| template | n | Top-1 entity | Top-3 entity | Top-1 brand | Top-3 brand | SKU | Auto-accept | Precision |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| hi_price_didi | 9 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 66.7% | 100.0% |
| hinglish_price | 8 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 62.5% | 100.0% |
| hi_generic_bhaiya | 8 | 87.5% | 100.0% | 100.0% | 100.0% | 75.0% | 62.5% | 100.0% |
| en_generic | 9 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 88.9% | 100.0% |
| hi_janaushadhi | 8 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| en_price | 8 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 87.5% | 100.0% |
| hi_generic_jagah | 9 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 66.7% | 100.0% |
| hi_verify_doctor | 8 | 87.5% | 100.0% | 100.0% | 100.0% | 87.5% | 87.5% | 85.7% |
| hi_price_kitne | 8 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 75.0% | 100.0% |
| hinglish_generic | 8 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| en_verify | 8 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 62.5% | 100.0% |
| hi_dosage | 9 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 77.8% | 100.0% |

## Confidence vs correctness

| top score | n | top-1 entity correct |
|---|---:|---:|
| 0.00-0.70 | 17 | 100.0% |
| 0.70-0.80 | 13 | 100.0% |
| 0.80-0.85 | 32 | 100.0% |
| 0.85-0.90 | 29 | 100.0% |
| 0.90-0.95 | 126 | 93.7% |
| 0.95-1.00 | 183 | 100.0% |

## Threshold sweep (T1 = min top score, T2 = min margin over #2)

Recommended operating points (highest auto-accept rate at each precision floor):

| min precision | T1 | T2 | auto-accept | precision | wrong auto-accepts | confirmation rate | truth offered in confirmation |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 100% | 0.72 | 0.12 | 78.2% | 100.0% | 0 | 21.8% | 100.0% |
| 99% | 0.72 | 0.12 | 78.2% | 100.0% | 0 | 21.8% | 100.0% |
| 98% | 0.72 | 0.02 | 89.0% | 98.9% | 4 | 11.0% | 100.0% |
| 95% | 0.72 | 0.02 | 89.0% | 98.9% | 4 | 11.0% | 100.0% |

Current setting T1=0.8, T2=0.06: auto-accept 77.8%, precision 98.7%, 4 wrong auto-accepts, confirmation 22.2%.

Accepted precision / auto-accept rate by (T1 row, T2 column):

| T1 \ T2 | 0.02 | 0.04 | 0.06 | 0.08 | 0.10 | 0.12 |
|---|---:|---:|---:|---:|---:|---:|
| 0.70 | 98.9% / 89.0% | 98.9% / 86.8% | 98.8% / 80.8% | 100.0% / 78.2% | 100.0% / 78.2% | 100.0% / 78.2% |
| 0.72 | 98.9% / 89.0% | 98.9% / 86.8% | 98.8% / 80.8% | 100.0% / 78.2% | 100.0% / 78.2% | 100.0% / 78.2% |
| 0.74 | 98.9% / 87.5% | 98.8% / 85.2% | 98.7% / 79.2% | 100.0% / 76.8% | 100.0% / 76.8% | 100.0% / 76.8% |
| 0.76 | 98.9% / 87.5% | 98.8% / 85.2% | 98.7% / 79.2% | 100.0% / 76.8% | 100.0% / 76.8% | 100.0% / 76.8% |
| 0.78 | 98.8% / 86.2% | 98.8% / 84.0% | 98.7% / 78.2% | 100.0% / 75.8% | 100.0% / 75.8% | 100.0% / 75.8% |
| 0.80 | 98.8% / 85.8% | 98.8% / 83.5% | 98.7% / 77.8% | 100.0% / 75.5% | 100.0% / 75.5% | 100.0% / 75.5% |
| 0.82 | 98.8% / 83.5% | 98.8% / 81.2% | 98.7% / 75.5% | 100.0% / 73.2% | 100.0% / 73.2% | 100.0% / 73.2% |
| 0.84 | 98.8% / 81.0% | 98.7% / 78.8% | 98.6% / 73.0% | 100.0% / 70.8% | 100.0% / 70.8% | 100.0% / 70.8% |
| 0.86 | 98.7% / 77.5% | 98.7% / 75.2% | 98.6% / 70.2% | 100.0% / 68.0% | 100.0% / 68.0% | 100.0% / 68.0% |
| 0.88 | 98.6% / 73.8% | 98.6% / 71.5% | 98.5% / 66.5% | 100.0% / 64.2% | 100.0% / 64.2% | 100.0% / 64.2% |
| 0.90 | 98.6% / 70.8% | 98.5% / 68.5% | 98.4% / 63.5% | 100.0% / 61.3% | 100.0% / 61.3% | 100.0% / 61.3% |

The pipeline asks for confirmation in more cases than this matcher rule alone: a look-alike (LASA) partner, or a brand heard without a strength when the family has several SKUs.

## End-to-end decision (Pipeline.query, typed text, mock TTS)

| outcome | share |
|---|---:|
| answered_correct | 61.0% |
| asked_to_confirm_truth_offered | 28.0% |
| needs_image_correct | 11.0% |

Statuses: ok 61, needs_confirmation 28, needs_image 11

Verdicts: GENERIC_EQUIVALENT 44, MATCH 15, BANNED_COMBINATION 2

## Latency

| stage | p50 ms | p95 ms |
|---|---:|---:|
| entity extraction + match (in-process) | 0.54 | 1.83 |

## Top-1 entity misses (first 20)

| id | condition | transcript | truth | top-1 (score) |
|---|---|---|---|---|
| u061 | asr_noise=0 | डॉक्टर ने ग्लाइकोमेट जीपी एक लिखा है ये सही है क्या | Glycomet GP 1 Tablet | Glycomet 500 Tablet (0.9) |
| u090 | asr_noise=0 | भैया मुझे कालपोल 650 गोली का सस्ता जेनेरिक बताओ | Calpol 650 Tablet | Calpol 500 Tablet (0.925) |
| u061 | asr_noise=0.1 | डॉक्टर ने ग्लाइकोमेट जीबी एक लिखा है ये सही है क्या | Glycomet GP 1 Tablet | Glycomet 500 Tablet (0.9) |
| u090 | asr_noise=0.1 | भैया मुझे कालपोल 650 गोली का सस्ता जेनेरिक बताओ | Calpol 650 Tablet | Calpol 500 Tablet (0.925) |
| u061 | asr_noise=0.2 | डॉक्टर ने ग्लाइकोमेट जीप एक लिखा है ये सही है क्या | Glycomet GP 1 Tablet | Glycomet 500 Tablet (0.9) |
| u090 | asr_noise=0.2 | भैया मुझे कालपोल छह सौ पचास गोली का सस्ता जेनेरिक बताओ | Calpol 650 Tablet | Calpol 500 Tablet (0.925) |
| u061 | asr_noise=0.3 | डॉक्टर ने ग्लाइकोमेट जीप एक लिखा है ये सही है क्या | Glycomet GP 1 Tablet | Glycomet 500 Tablet (0.9) |
| u090 | asr_noise=0.3 | भैया मुझे कालपोल 650 गोली का सस्ता जेनेरिक बताओ | Calpol 650 Tablet | Calpol 500 Tablet (0.925) |
