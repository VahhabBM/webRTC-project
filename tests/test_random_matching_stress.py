import random
import time
from uuid import UUID

from apps.events.matching import (
    GeneratedSchedule,
    MatchInput,
    MatchParticipant,
    ScheduleRound,
    generate_schedule,
    validate_schedule,
)
from apps.events.scoring import ParticipantProfile, ScoringWeights


def _make_pid(n: int) -> str:
    """تولید شناسه UUID قطعی برای شرکت‌کننده شماره n"""
    return str(UUID(int=n))


def _build_random_input(
    n_participants: int,
    num_rounds: int,
    rng: random.Random,
) -> MatchInput:
    """ساخت MatchInput با پروفایل و تگ‌های تصادفی"""
    available_tags = [f"tag_{t}" for t in range(1, 10)]
    parts = []
    for i in range(1, n_participants + 1):
        num_tags = rng.randint(0, 3)
        tags = frozenset(rng.sample(available_tags, num_tags))
        parts.append(
            MatchParticipant(
                pid=_make_pid(i),
                profile=ParticipantProfile(tag_ids=tags),
            )
        )

    # مرتب‌سازی بر اساس pid مطابق استاندارد ورودی موتور
    parts.sort(key=lambda p: p.pid)

    tag_weight = round(rng.uniform(0.1, 2.0), 2)
    return MatchInput(
        participants=tuple(parts),
        num_rounds=num_rounds,
        weights=ScoringWeights(tag_weight=tag_weight),
    )


class TestRandomMatchingStressT46:
    """مجموعه تست T-46: استرس ۱۰۰۰ اجرای تصادفی بدون نقض قید و اعتبارسنجی شکست در تکرار"""

    def test_1000_random_matching_runs_zero_violations(self):
        """اجرای ۱۰۰۰ سناریوی تصادفی با اندازه‌های زوج و فرد و تضمین نقض قید صفر"""
        rng = random.Random(42)  # Seed مشخص جهت تکرارپذیری
        total_iterations = 1000

        even_count = 0
        odd_count = 0

        start_time = time.time()

        for iteration in range(1, total_iterations + 1):
            # انتخاب تصادفی تعداد شرکت‌کنندگان (حداقل ۲ و پوشش کامل زوج و فرد)
            n = rng.randint(2, 36)
            if n % 2 == 0:
                even_count += 1
                max_rounds = min(n - 1, 5)
            else:
                odd_count += 1
                max_rounds = min(n, 5)

            num_rounds = rng.randint(1, max(1, max_rounds))

            match_input = _build_random_input(n, num_rounds, rng)
            schedule = generate_schedule(match_input)

            pids = frozenset(mp.pid for mp in match_input.participants)
            violations = validate_schedule(schedule, pids, num_rounds=num_rounds)

            # هر اجرا باید دقیقاً نقض قید صفر داشته باشد
            assert violations == [], (
                f"Violation detected in iteration {iteration} with N={n}, R={num_rounds}: {violations}"
            )

        elapsed_time = time.time() - start_time

        # اعتبارسنجی شرایط آزمون
        assert even_count > 0, "Even participant counts must be tested"
        assert odd_count > 0, "Odd participant counts must be tested"
        # زمان کل باید بسیار کمتر از محدودیت ۵ دقیقه‌ای (۳۰۰ ثانیه) CI باشد
        assert elapsed_time < 300, (
            f"CI execution time exceeded 5 minutes: {elapsed_time:.2f}s"
        )

    def test_intentionally_corrupted_schedule_fails_validation(self):
        """معیار پذیرش (DoD): یک اجرای عمداً خراب با تکرار اجباری شریک باید با خطا شناسایی شود"""
        rng = random.Random(101)
        n = 6
        num_rounds = 2
        match_input = _build_random_input(n, num_rounds, rng)
        valid_schedule = generate_schedule(match_input)

        pids = frozenset(mp.pid for mp in match_input.participants)
        assert validate_schedule(valid_schedule, pids, num_rounds=num_rounds) == []

        # عمداً جفت دور دوم را با همان جفت دور اول خراب می‌کنیم (تکرار اجباری شریک)
        r1 = valid_schedule.rounds[0]
        forced_repeated_pair = r1.pairs[0]

        # ساخت دور دوم که عمداً شامل جفت تکراری از دور اول است
        corrupted_round_2_pairs = list(valid_schedule.rounds[1].pairs)
        corrupted_round_2_pairs[0] = forced_repeated_pair

        corrupted_round_2 = ScheduleRound(
            number=2,
            pairs=tuple(corrupted_round_2_pairs),
            unmatched_pid=valid_schedule.rounds[1].unmatched_pid,
        )

        corrupted_schedule = GeneratedSchedule(rounds=(r1, corrupted_round_2))

        violations = validate_schedule(corrupted_schedule, pids, num_rounds=num_rounds)

        # اعتبارسنجی اینکه خطا شناسایی شد و تست عمداً قرمز/ناموفق بودن تکرار را اثبات می‌کند
        assert len(violations) > 0, "Validator must reject repeated partner"
        violation_kinds = {v.kind for v in violations}
        assert "REPEATED_PARTNER" in violation_kinds, (
            f"Expected REPEATED_PARTNER violation, got: {violation_kinds}"
        )
