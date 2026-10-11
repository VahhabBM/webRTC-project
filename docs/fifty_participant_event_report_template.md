# 50-Person Event Report Template (تسک ۵۴-T)

## 1. Event Overview & Attendance (اطلاعات کلی و تعداد حاضرین)
- **Date / Time:** [Insert Date]
- **Server Location:** Ireland Server (سرور ایرلند)
- **Total Registered Participants:** 50
- **Total Confirmed Attendees Present:** [تعداد حاضر]

---

## 2. Telemetry & Connection Metrics (تله‌متری و معیارهای اتصال)

### Connection Rate per Round (نرخ اتصال هر راند)
* آستانه استاندارد تایید راند اول: **$\ge 85\%$**
- **Round 1 Connection Rate:** [درصد موفقیت اتصال - مثلاً 88%] 
- **Round 2 Connection Rate:** [درصد]
- **Round 3 Connection Rate:** [درصد]
- **Round 4 Connection Rate:** [درصد]
- **Round 5 Connection Rate:** [درصد]
- **Round 6 Connection Rate:** [درصد]

### Disconnection & Recovery Stats (قطع و وصل‌های بازیابی‌شده)
- **Total Temporary Disconnections:** [تعداد کل قطع ارتباط‌های موقت]
- **Successfully Recovered Disconnections:** [تعداد قطع ارتباط‌های بازیابی‌شده]

---

## 3. Stop Conditions & Threshold Evaluation (معیارهای توقف و ارزیابی آستانه)

- **First Round Success Threshold:** 85%
- **Round 1 Result Evaluation:** 
  - [ ] **PASS ($\ge 85\%$):** رویداد مجاز به ادامه در راندهای بعدی است.
  - [ ] **FAIL ($< 85\%$):** رویداد باید متوقف گردد (Stop Condition Triggered).

---

## 4. Strict Operational Rules & Guidelines (قوانین و محدودیت‌های اجرایی)

1. **Manual Matching Prohibition:** 
   > **اخطار:** جفت‌سازی دستی (Manual Matching) به هر شکل و دلیل اکیداً ممنوع است و تمامی جفت‌سازی‌ها باید صرفاً از طریق سیستم خودکار پلتفرم انجام گیرد.
2. **Mandatory Stop Condition:** 
   > در صورتی که نرخ اتصال راند اول کمتر از آستانه ۸۵٪ باشد، برگزاری رویداد باید فوراً متوقف و لغو گردد.

---
*Note: This is an empty report template to be filled by the server administrator/operator post-event.*