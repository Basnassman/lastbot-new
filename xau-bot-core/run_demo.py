"""
شغّل هذا الملف لرؤية دورة قرار كاملة ببيانات تجريبية.
الهدف: تتأكد أن المنطق يعمل ومفهوم لك بالكامل قبل ربطه بأي شيء حي.

الاستخدام: python run_demo.py
"""
from brain.data.mock_price_feed import MockCorrelationFeed, MockNewsFeed, MockPriceFeed
from brain.decision_engine import DecisionEngine


def main() -> None:
    engine = DecisionEngine(
        price_feed=MockPriceFeed(),
        news_feed=MockNewsFeed(),
        correlation_feed=MockCorrelationFeed(),
    )

    print("=" * 60)
    print("تشغيل 5 دورات قرار تجريبية متتالية")
    print("=" * 60)

    for i in range(1, 6):
        decision = engine.decide()
        print(f"\n--- الدورة {i} ---")
        print(f"القرار: {decision.action}")
        print(f"حجم المخاطرة: ${decision.position_size_usd}")
        print(f"مستوى الثقة: {decision.confidence:.1f}")
        print("الأسباب:")
        for reason in decision.reasons:
            print(f"  - {reason}")

    print("\n" + "=" * 60)
    print("سجل القرارات الكامل محفوظ في: logs/decisions.jsonl")
    print("=" * 60)


if __name__ == "__main__":
    main()
