"""
Step 4: database logging for X-ray analyses.

Setup:
    pip install mysql-connector-python python-dotenv

Add to your .env (create one if it doesn't exist):
    MYSQL_HOST=localhost
    MYSQL_USER=mcp_user
    MYSQL_PASSWORD=0000
    MYSQL_DATABASE=radiology_copilot_db
"""

import os

import mysql.connector
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        user=os.getenv("MYSQL_USER", "mcp_user"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE", "radiology_copilot_db"),
    )


def init_db() -> None:
    """Create all tables if they don't already exist."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS xray_studies (
                id INT AUTO_INCREMENT PRIMARY KEY,
                filename VARCHAR(255) NOT NULL,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS cv_findings (
                id INT AUTO_INCREMENT PRIMARY KEY,
                study_id INT NOT NULL,
                pathology VARCHAR(100) NOT NULL,
                probability FLOAT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (study_id) REFERENCES xray_studies(id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()
        cursor.close()
    finally:
        conn.close()


def log_study(filename: str, findings: dict) -> int:
    """Save a study and all its findings. Returns the new study id."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO xray_studies (filename) VALUES (%s)", (filename,)
        )
        study_id = cursor.lastrowid

        for pathology, probability in findings.items():
            cursor.execute(
                "INSERT INTO cv_findings (study_id, pathology, probability) VALUES (%s, %s, %s)",
                (study_id, pathology, probability),
            )

        conn.commit()
        cursor.close()
        return study_id
    finally:
        conn.close()


def get_study(study_id: int):
    """Return a study and its findings, or None if not found."""
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM xray_studies WHERE id = %s", (study_id,))
        study = cursor.fetchone()
        if study is None:
            return None

        cursor.execute(
            "SELECT pathology, probability FROM cv_findings WHERE study_id = %s ORDER BY probability DESC",
            (study_id,),
        )
        study["findings"] = cursor.fetchall()
        cursor.close()
        return study
    finally:
        conn.close()


def list_studies():
    """Return all studies, most recent first."""
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM xray_studies ORDER BY id DESC")
        studies = cursor.fetchall()
        cursor.close()
        return studies
    finally:
        conn.close()


def init_evidence_table() -> None:
    """Create the evidence_citations table if it doesn't already exist."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_citations (
                id INT AUTO_INCREMENT PRIMARY KEY,
                study_id INT NOT NULL,
                pathology VARCHAR(100) NOT NULL,
                source_text TEXT NOT NULL,
                source_reference VARCHAR(500),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (study_id) REFERENCES xray_studies(id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()
        cursor.close()
    finally:
        conn.close()


def log_evidence(study_id: int, pathology: str, source_text: str, source_reference: str) -> int:
    """Save one retrieved evidence passage for a study. Returns the new row id."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO evidence_citations (study_id, pathology, source_text, source_reference) "
            "VALUES (%s, %s, %s, %s)",
            (study_id, pathology, source_text, source_reference),
        )
        conn.commit()
        new_id = cursor.lastrowid
        cursor.close()
        return new_id
    finally:
        conn.close()


def get_evidence_for_study(study_id: int):
    """Return all evidence citations logged for a study."""
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM evidence_citations WHERE study_id = %s ORDER BY id",
            (study_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        conn.close()


def init_report_table() -> None:
    """Create the agent_reports table if it doesn't already exist."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_reports (
                id INT AUTO_INCREMENT PRIMARY KEY,
                study_id INT NOT NULL,
                report_text TEXT NOT NULL,
                status VARCHAR(50) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (study_id) REFERENCES xray_studies(id) ON DELETE CASCADE
            )
            """
        )
        # Self-heal databases whose agent_reports predates the status column:
        # CREATE TABLE IF NOT EXISTS never alters an existing table, which
        # is exactly how the schema.sql/init_report_table drift happened.
        cursor.execute("SHOW COLUMNS FROM agent_reports LIKE 'status'")
        if cursor.fetchone() is None:
            cursor.execute(
                "ALTER TABLE agent_reports ADD COLUMN status VARCHAR(50) NOT NULL DEFAULT ''"
            )
        conn.commit()
        cursor.close()
    finally:
        conn.close()


def log_report(study_id: int, report_text: str, status: str) -> int:
    """Save a synthesized report. Returns the new report id."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agent_reports (study_id, report_text, status) VALUES (%s, %s, %s)",
            (study_id, report_text, status),
        )
        conn.commit()
        new_id = cursor.lastrowid
        cursor.close()
        return new_id
    finally:
        conn.close()


def init_clinician_review_table() -> None:
    """Create the clinician_reviews table if it doesn't already exist."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS clinician_reviews (
                id INT AUTO_INCREMENT PRIMARY KEY,
                report_id INT NOT NULL,
                reviewer_name VARCHAR(255),
                decision ENUM('approved', 'edited', 'rejected') NOT NULL,
                notes TEXT,
                reviewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (report_id) REFERENCES agent_reports(id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()
        cursor.close()
    finally:
        conn.close()


def log_clinician_review(report_id: int, reviewer_name: str, decision: str, notes: str = "") -> int:
    """Save a clinician's review decision for a report. Returns the new review id."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO clinician_reviews (report_id, reviewer_name, decision, notes) "
            "VALUES (%s, %s, %s, %s)",
            (report_id, reviewer_name, decision, notes),
        )
        conn.commit()
        new_id = cursor.lastrowid
        cursor.close()
        return new_id
    finally:
        conn.close()


def get_reviews_for_report(report_id: int):
    """Return all review decisions logged for a report."""
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM clinician_reviews WHERE report_id = %s ORDER BY id",
            (report_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("Tables ready. Existing studies:", list_studies())