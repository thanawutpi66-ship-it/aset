"""
train_grader.py — สคริปต์ train โมเดล grader จาก labeled CSV

ใช้ทีหลังเมื่อมีข้อมูล labeled แล้ว:
    แต่ละไฟล์ test CSV จะถูกสกัด features ด้วย BatteryAnalyzer แล้ว map กับ label
    เทรน RandomForestClassifier และบันทึกเป็น .joblib ให้ BatteryGrader โหลดใช้

รูปแบบ labels file (CSV):
    csv_path,grade
    data/cell_001.csv,A
    data/cell_002.csv,B
    ...
(path สัมพัทธ์จะอ้างอิงโฟลเดอร์ของ labels file)

ต้องมี: scikit-learn, joblib
    pip install scikit-learn joblib

การใช้งาน:
    python train_grader.py labels.csv -o grader_model.joblib
    python train_grader.py labels.csv --rated-capacity 2.0 --test-size 0.2
"""
import argparse
import csv
import logging
import os
import sys
from typing import List, Tuple

import numpy as np

from analysis_module import BatteryAnalyzer, FEATURE_NAMES

logger = logging.getLogger(__name__)


def _read_labels(labels_path: str) -> List[Tuple[str, str]]:
    """อ่าน labels file -> list ของ (csv_path_absolute, grade)"""
    base_dir = os.path.dirname(os.path.abspath(labels_path))
    rows: List[Tuple[str, str]] = []
    with open(labels_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "csv_path" not in reader.fieldnames \
                or "grade" not in reader.fieldnames:
            raise ValueError("labels file ต้องมีคอลัมน์ 'csv_path' และ 'grade'")
        for row in reader:
            path = row["csv_path"].strip()
            grade = row["grade"].strip()
            if not path or not grade:
                continue
            if not os.path.isabs(path):
                path = os.path.join(base_dir, path)
            rows.append((path, grade))
    return rows


def build_dataset(labels_path: str, rated_capacity_ah: float
                  ) -> Tuple[np.ndarray, np.ndarray]:
    """สกัด features จากทุกไฟล์ -> (X, y)"""
    analyzer = BatteryAnalyzer(rated_capacity_ah=rated_capacity_ah)
    labels = _read_labels(labels_path)
    if not labels:
        raise ValueError("ไม่มีข้อมูลใน labels file")

    X: List[np.ndarray] = []
    y: List[str] = []
    for path, grade in labels:
        result = analyzer.analyze(path)
        if not result.success or result.features is None:
            logger.warning("ข้าม %s: %s", path, result.error or "no features")
            continue
        X.append(result.features.to_vector())
        y.append(grade)

    if not X:
        raise ValueError("สกัด features ไม่ได้เลยสักไฟล์")

    logger.info("สร้าง dataset: %d ตัวอย่าง, %d features", len(X), len(FEATURE_NAMES))
    return np.vstack(X), np.asarray(y)


def train(labels_path: str, output_path: str, rated_capacity_ah: float,
          test_size: float, n_estimators: int, random_state: int) -> None:
    """เทรนและบันทึกโมเดล"""
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import classification_report
        import joblib
    except ImportError as e:
        raise SystemExit(
            "ต้องติดตั้ง scikit-learn และ joblib ก่อน:\n"
            "    pip install scikit-learn joblib\n"
            f"(import error: {e})"
        )

    X, y = build_dataset(labels_path, rated_capacity_ah)

    n_classes = len(set(y))
    logger.info("จำนวนคลาส: %d -> %s", n_classes, sorted(set(y)))

    # ถ้ามีข้อมูลพอและหลายคลาส ค่อยแบ่ง train/test
    can_split = len(y) >= 5 and test_size > 0 and n_classes > 1
    if can_split:
        stratify = y if all(list(y).count(c) >= 2 for c in set(y)) else None
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=random_state, stratify=stratify
        )
    else:
        logger.warning("ข้อมูลน้อย — เทรนด้วยทั้งหมด ไม่แยก test set")
        X_train, y_train = X, y
        X_test, y_test = None, None

    model = RandomForestClassifier(
        n_estimators=n_estimators, random_state=random_state, class_weight="balanced"
    )
    model.fit(X_train, y_train)

    if X_test is not None:
        y_pred = model.predict(X_test)
        logger.info("ผลบน test set:\n%s",
                    classification_report(y_test, y_pred, zero_division=0))

    # แสดงความสำคัญของ feature
    importances = sorted(zip(FEATURE_NAMES, model.feature_importances_),
                         key=lambda kv: kv[1], reverse=True)
    logger.info("Feature importances:")
    for name, imp in importances:
        logger.info("    %-16s %.4f", name, imp)

    joblib.dump(model, output_path)
    logger.info("บันทึกโมเดลที่ %s", output_path)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Train battery grader model จาก labeled CSV"
    )
    parser.add_argument("labels", help="path ของ labels file (csv_path,grade)")
    parser.add_argument("-o", "--output", default="grader_model.joblib",
                        help="path ไฟล์โมเดลที่จะบันทึก (default: grader_model.joblib)")
    parser.add_argument("--rated-capacity", type=float, default=2.0,
                        help="ความจุที่ rate ไว้ (Ah) สำหรับคำนวณ SoH (default: 2.0)")
    parser.add_argument("--test-size", type=float, default=0.2,
                        help="สัดส่วน test set (default: 0.2, 0 = ไม่แยก)")
    parser.add_argument("--n-estimators", type=int, default=200,
                        help="จำนวนต้นไม้ใน RandomForest (default: 200)")
    parser.add_argument("--random-state", type=int, default=42,
                        help="random seed (default: 42)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s - %(name)s - %(message)s",
    )

    try:
        train(args.labels, args.output, args.rated_capacity,
              args.test_size, args.n_estimators, args.random_state)
    except (ValueError, FileNotFoundError) as e:
        logger.error("เทรนล้มเหลว: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
