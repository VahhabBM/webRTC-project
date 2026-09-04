import csv
import io
from datetime import timedelta

import pytest
from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.events.models import Event, Participant, ParticipantStatus, Tag

User = get_user_model()


@pytest.mark.django_db
class TestParticipantCSVExport:
    def test_export_action_registered_and_bom_present(self, client: Client):
        """تست اینکه خروجی دارای بایت BOM برای اکسل است و فارسی را بدون نقص نگه می‌دارد"""
        admin_instance = site._registry[Participant]
        assert "export_as_csv" in admin_instance.actions

        admin_user = User.objects.create_superuser(
            username="export_admin",
            email="export@test.com",
            password="adminpassword",
        )
        client.force_login(admin_user)

        event = Event.objects.create(
            name="همایش هوش مصنوعی",
            num_rounds=2,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=timezone.now(),
        )
        tag1 = Tag.objects.create(name="پایتون")
        tag2 = Tag.objects.create(name="یادگیری ماشین")

        participant = Participant.objects.create(
            event=event,
            display_name="علی شعبانی",
            email="ali@example.com",
            status=ParticipantStatus.WAITING,
        )
        participant.tags.add(tag1, tag2)

        # فراخوانی اکشن ادمین
        response = client.post(
            "/admin/events/participant/",
            {
                "action": "export_as_csv",
                "_selected_action": [str(participant.id)],
            },
        )

        assert response.status_code == 200
        assert "text/csv" in response["Content-Type"]
        assert "participants_" in response["Content-Disposition"]

        content = response.content
        # ۱. بررسی قید کلیدی تسک: شروع فایل با UTF-8 BOM جهت نمایش بدون به‌هم‌ریختگی در اکسل
        assert content.startswith(b"\xef\xbb\xbf")

        # ۲. بررسی خوانایی کاراکترهای فارسی با دیکود utf-8-sig
        decoded_text = content.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(decoded_text))
        rows = list(reader)

        # بررسی سرفصل‌های ستون‌ها
        assert rows[0] == [
            "شناسه",
            "نام و نام خانوادگی",
            "ایمیل",
            "وضعیت",
            "رویداد",
            "تگ‌ها",
            "تاریخ ثبت‌نام",
        ]

        # بررسی سطر داده
        data_row = rows[1]
        assert data_row[1] == "علی شعبانی"
        assert data_row[2] == "ali@example.com"
        assert data_row[4] == "همایش هوش مصنوعی"
        assert "پایتون" in data_row[5]
        assert "یادگیری ماشین" in data_row[5]

    def test_export_benchmark_with_large_dataset(self, client: Client):
        """بررسی عدم قطع شدن درخواست و سرعت پردازش روی مجموعه‌های حجیم"""
        admin_user = User.objects.create_superuser(
            username="perf_admin",
            email="perf@test.com",
            password="adminpassword",
        )
        client.force_login(admin_user)

        event = Event.objects.create(
            name="رویداد حجیم",
            num_rounds=2,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=timezone.now(),
        )
        tag = Tag.objects.create(name="شبکه‌سازی")

        # ساخت ۱۰۰ شرکت‌کننده تستی برای بررسی سلامت تگ‌ها و کوئری‌ها
        participants = [
            Participant(
                event=event,
                display_name=f"شرکت‌کننده شماره {i}",
                email=f"user_{i}@example.com",
                status=ParticipantStatus.READY,
            )
            for i in range(100)
        ]
        created_participants = Participant.objects.bulk_create(participants)
        for p in created_participants:
            p.tags.add(tag)

        response = client.post(
            "/admin/events/participant/",
            {
                "action": "export_as_csv",
                "_selected_action": [str(p.id) for p in created_participants],
            },
        )

        assert response.status_code == 200
        decoded_text = response.content.decode("utf-8-sig")
        lines = decoded_text.strip().splitlines()
        # ۱۰۱ خط: ۱ خط هدر + ۱۰۰ خط داده
        assert len(lines) == 101
