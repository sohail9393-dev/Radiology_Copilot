-- Step 4: MySQL schema for logging every X-ray analysis.
-- Run this once the database exists, or let database.py's init_db() create it automatically.

CREATE DATABASE IF NOT EXISTS radiology_copilot_db;

USE radiology_copilot_db;

CREATE TABLE IF NOT EXISTS xray_studies (
    id INT AUTO_INCREMENT PRIMARY KEY,
    filename VARCHAR(255) NOT NULL,
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cv_findings (
    id INT AUTO_INCREMENT PRIMARY KEY,
    study_id INT NOT NULL,
    pathology VARCHAR(100) NOT NULL,
    probability FLOAT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (study_id) REFERENCES xray_studies(id) ON DELETE CASCADE
);

-- Tables for later steps (created now so the schema is complete, but unused until steps 5-7):

CREATE TABLE IF NOT EXISTS evidence_citations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    study_id INT NOT NULL,
    pathology VARCHAR(100) NOT NULL,
    source_text TEXT NOT NULL,
    source_reference VARCHAR(500),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (study_id) REFERENCES xray_studies(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_reports (
    id INT AUTO_INCREMENT PRIMARY KEY,
    study_id INT NOT NULL,
    report_text TEXT NOT NULL,
    -- Must match database.py's init_report_table(): log_report() inserts this column.
    status VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (study_id) REFERENCES xray_studies(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS clinician_reviews (
    id INT AUTO_INCREMENT PRIMARY KEY,
    report_id INT NOT NULL,
    reviewer_name VARCHAR(255),
    decision ENUM('approved', 'edited', 'rejected') NOT NULL,
    notes TEXT,
    reviewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (report_id) REFERENCES agent_reports(id) ON DELETE CASCADE
);
