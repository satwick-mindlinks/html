import re
import json
import secrets
import string
import shutil
import tempfile
import requests
import uuid
import io
from datetime import datetime, timezone, timedelta, date
import os
from flask import Flask, request, jsonify, url_for, send_file, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, case, and_
from flask_cors import CORS,cross_origin
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
from functools import wraps
from werkzeug.utils import secure_filename
import logging
import mimetypes
import time
import anthropic
from alembic import op
from azure.core.credentials import AzureKeyCredential
from azure.ai.formrecognizer import DocumentAnalysisClient
from flask_mail import Mail, Message
from collections import defaultdict
from datetime import datetime, timezone
from flask import jsonify, request
from sqlalchemy.exc import SQLAlchemyError
import traceback
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import desc, or_, and_
from flask_socketio import SocketIO, emit, join_room, leave_room
import yaml
from PIL import Image
import PyPDF2
def load_email_templates():
    """Load email templates from config file"""
    with open('config(TDC).yaml', 'r') as f:
        config = yaml.safe_load(f)
    return config.get('EMAIL_TEMPLATES', {})
def load_config():
    with open('Config(TDC).yaml', 'r') as file:
        config = yaml.safe_load(file)
        return config

config = load_config()

app = Flask(__name__)
# Configure Flask app
app.config['SECRET_KEY'] = config['FLASK']['SECRET_KEY']
app.config['FRONTEND_URL'] = config['FLASK']['FRONTEND_URL']

socketio = SocketIO(app, cors_allowed_origins="*")



CORS(app, resources={
    r"/*": {
        "origins": ["http://localhost:3000"],
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization", "Accept"],
        "expose_headers": ["Content-Type", "Authorization"],
        "supports_credentials": True
    }
})
# Configure database
app.config['SQLALCHEMY_DATABASE_URI'] = f"postgresql://{config['DATABASE']['USERNAME']}:{config['DATABASE']['PASSWORD']}@{config['DATABASE']['HOST']}/{config['DATABASE']['NAME']}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = config['DATABASE']['TRACK_MODIFICATIONS']

app.config['UPLOAD_FOLDER'] = config['FOLDERS']['MAIN']
app.config['OCR_FOLDER'] = config['FOLDERS']['OCR']
app.config['EXTRACTED_CLAUDE'] = config['FOLDERS']['EXTRACTED_CLAUDE']
app.config['EXTRACTED_PARSED'] = config['FOLDERS']['EXTRACTED_PARSED']

# Configure email
app.config['MAIL_SERVER'] = config['EMAIL']['MAIL_SERVER']
app.config['MAIL_PORT'] = config['EMAIL']['MAIL_PORT']
app.config['MAIL_USE_TLS'] = config['EMAIL']['MAIL_USE_TLS']
app.config['MAIL_USERNAME'] = config['EMAIL']['MAIL_USERNAME']
app.config['MAIL_PASSWORD'] = config['EMAIL']['MAIL_PASSWORD']
app.config['MAIL_DEFAULT_SENDER'] = config['EMAIL']['MAIL_DEFAULT_SENDER']
mail = Mail(app)

# Ensure output folders exist
for folder in [app.config['UPLOAD_FOLDER'], app.config['OCR_FOLDER'], app.config['EXTRACTED_CLAUDE'],
               app.config['EXTRACTED_PARSED']]:
    os.makedirs(folder, exist_ok=True)

db = SQLAlchemy(app)

class Tenant(db.Model):
    __tablename__ = 'tenants'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    subdomain = db.Column(db.String(255), unique=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime, default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc))

# Users model
class User(db.Model):
    __tablename__ = 'users'
    user_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(50), unique=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc),
                                   onupdate=datetime.now(timezone.utc))
    user_role = db.Column(db.Enum('customer', 'support_agent', 'administrator', 'super_admin', name='user_role_enum'),
                          default='customer')
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'))
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(50), nullable=False)
    last_login = db.Column(db.DateTime)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    declaration_token = db.Column(db.String(255), unique=True)
    is_declared = db.Column(db.Boolean, default=False)

    magic_link = db.Column(db.String(255), unique=True)
    magic_link_status = db.Column(db.Enum('active', 'expired', 'used', name='magic_link_status_enum'))
    magic_link_created_date = db.Column(db.DateTime(timezone=True))
    magic_link_expiry_date = db.Column(db.DateTime(timezone=True))

    @property
    def is_admin(self):
        return self.user_role in ['administrator', 'super_admin']  # Check if the role is admin or higher

    @property
    def is_support_agent(self):
        return self.user_role == 'support_agent'

    @property
    def is_super_admin(self):
        return self.user_role == 'super_admin'

class Document(db.Model):
    __tablename__ = 'documents'

    document_id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False)
    document_type_id = db.Column(db.Integer, db.ForeignKey('document_types.document_type_id'))
    file_name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(512), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)
    mime_type = db.Column(db.String(100), nullable=False)

    upload_date = db.Column(db.DateTime(timezone=True), nullable=True)  # Changed to not nullable
    process_status = db.Column(db.String(20), default='PENDING')
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc),
                           onupdate=datetime.now(timezone.utc))

    status = db.Column(db.String(50), nullable=False)  # Changed length to 50
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'))
    tax_year = db.Column(db.Integer, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))

    requirement_source = db.Column(db.String(50), nullable=True)
    source_reference_id = db.Column(db.Integer, nullable=True)
    due_date = db.Column(db.Date, nullable=True)

    priority = db.Column(db.String(20), nullable=True)
    waiver_reason = db.Column(db.Text, nullable=True)
    waiver_approved_by = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=True)
    original_file = db.Column(db.LargeBinary)
    is_deleted = db.Column(db.Boolean, nullable=True)
    del_reason_code = db.Column(db.String(100), nullable=True)
    delete_comments = db.Column(db.Text)
    customer_taxfinancial = db.Column(db.Integer, nullable=False)
    # New Columns
    customer_entered_due_date = db.Column(db.DateTime(timezone=True), nullable=True)
    customer_due_date_comments = db.Column(db.String(1000), nullable=True)

    # Relationship with validation results
    validation_results = db.relationship('ValidationResult', back_populates='document')
    # Relationship with DocumentMessages
    messages = db.relationship('DocumentMessages', back_populates='document', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<Document {self.document_id}: {self.file_name}>'

class DocumentMessages(db.Model):
    __tablename__ = 'document_messages'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    document_id = db.Column(db.Integer, db.ForeignKey('documents.document_id', ondelete='CASCADE'), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.user_id', ondelete='CASCADE'), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc),
                           onupdate=datetime.now(timezone.utc))
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=True)
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=True)

    # Relationship with Document
    document = db.relationship('Document', back_populates='messages')

    def __repr__(self):
        return f'<DocumentMessage {self.id}: {self.message[:50]}...>'

class DocumentType(db.Model):
    __tablename__ = 'document_types'

    document_type_id = db.Column(db.Integer, primary_key=True)
    type_name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.Text)
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now)
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    category_name = db.Column(db.String(100))  # Adjust length based on anticipated category names

    # Relationships
    validation_rules = db.relationship('ValidationRule', back_populates='document_type')

# Ad these new models for OCR results and extracted data
class OCRResult(db.Model):
    __tablename__ = 'ocr_results'
    ocr_result_id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('documents.document_id'))
    raw_text = db.Column(db.Text, nullable=False)
    confidence_score = db.Column(db.Numeric(5, 2), nullable=False)
    processing_time = db.Column(db.Integer, nullable=False)
    ocr_engine = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc),
                           onupdate=datetime.now(timezone.utc))
    status = db.Column(db.String(20), nullable=False)

class ExtractedData(db.Model):
    __tablename__ = 'extracted_data'
    extracted_data_id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('documents.document_id'))
    field_name = db.Column(db.String(255), nullable=False)
    field_value = db.Column(db.Text, nullable=False)
    confidence_score = db.Column(db.Numeric(5, 2), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc),
                           onupdate=datetime.now(timezone.utc))
    status = db.Column(db.String(20), nullable=False)

class Country(db.Model):
    __tablename__ = 'countries'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(3), nullable=False, unique=True)

class State(db.Model):
    __tablename__ = 'states'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    abbreviation = db.Column(db.String(10))
    country_id = db.Column(db.Integer, db.ForeignKey('countries.id'))
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc),
                           onupdate=datetime.now(timezone.utc))

class Customer(db.Model):
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'))
    marital_status = db.Column(db.String(50))
    address = db.Column(db.Text)
    city = db.Column(db.String(100))
    state_id = db.Column(db.Integer, db.ForeignKey('states.id'))
    country_id = db.Column(db.Integer, db.ForeignKey('countries.id'))
    zip_code = db.Column(db.String(20))
    occupation = db.Column(db.String(255))
    date_of_birth = db.Column(db.Date)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc),
                                   onupdate=datetime.now(timezone.utc))

class Dependent(db.Model):
    __tablename__ = 'dependents'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'))
    name = db.Column(db.String(255), nullable=False)
    relationship = db.Column(db.String(100), nullable=False)
    date_of_birth = db.Column(db.Date)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc),
                                   onupdate=datetime.now(timezone.utc))

class TaxReturnTimeline(db.Model):
    __tablename__ = 'tax_return_timelines'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'))
    tax_year = db.Column(db.Integer, nullable=False)
    document_upload_deadline = db.Column(db.Date)
    review_start_date = db.Column(db.Date)
    review_end_date = db.Column(db.Date)
    preparation_start_date = db.Column(db.Date)
    preparation_end_date = db.Column(db.Date)
    client_review_date = db.Column(db.Date)
    filing_date = db.Column(db.Date)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc),
                                   onupdate=datetime.now(timezone.utc))

class CustomerQuery(db.Model):
    __tablename__ = 'customer_queries'
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('documents.document_id'), nullable=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    recipient_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc),
                                   onupdate=datetime.now(timezone.utc))

class ValidationResult(db.Model):
    __tablename__ = 'validation_results'

    validation_result_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    document_id = db.Column(db.Integer, db.ForeignKey('documents.document_id'), nullable=True)
    rule_id = db.Column(db.Integer, db.ForeignKey('validation_rules.rule_id'), nullable=True)
    is_valid = db.Column(db.Boolean, nullable=False)
    validation_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), onupdate=db.func.now())

    # Relationships (assuming you have Document and ValidationRule models)
    document = db.relationship('Document', back_populates='validation_results')
    rule = db.relationship('ValidationRule', back_populates='validation_results')

class ValidationRule(db.Model):
    __tablename__ = 'validation_rules'

    rule_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    document_type_id = db.Column(db.Integer, db.ForeignKey('document_types.document_type_id'), nullable=True)
    field_name = db.Column(db.String(50), nullable=False)
    rule_type = db.Column(db.String(50), nullable=False)
    rule_value = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), onupdate=db.func.now())

    # Relationships
    document_type = db.relationship('DocumentType', back_populates='validation_rules')
    validation_results = db.relationship('ValidationResult', back_populates='rule')

    def __repr__(self):
        return f'<ValidationRule {self.rule_id}: {self.field_name} - {self.rule_type}>'

class DeclarationFormData(db.Model):
    """Model for storing form data during declaration process"""
    __tablename__ = 'declaration_form_data'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False)
    section = db.Column(db.String(50), nullable=False)
    data = db.Column(db.JSON, nullable=False)
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('user_id', 'section', name='unique_user_section'),
    )

class QuestionCategory(db.Model):
    __tablename__ = 'question_categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    display_order = db.Column(db.Integer)
    active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer)
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer)
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))


class TaxQuestion(db.Model):
    __tablename__ = 'tax_questions'
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('question_categories.id'))
    question_text = db.Column(db.Text, nullable=False)
    question_type = db.Column(db.String(50))
    help_text = db.Column(db.Text)
    validation_rules = db.Column(db.Text)
    required = db.Column(db.Boolean, default=False)
    display_order = db.Column(db.Integer)
    active = db.Column(db.Boolean, default=True)
    impacts_document_requirement = db.Column(db.Boolean)
    created_by = db.Column(db.Integer)
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer)
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))


class QuestionDocumentMapping(db.Model):
    __tablename__ = 'question_document_mappings'

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey('tax_questions.id'))
    document_type_id = db.Column(db.Integer, db.ForeignKey('document_types.document_type_id'))
    response_trigger = db.Column(db.String(50))  # Value that triggers document requirement (e.g., 'yes')
    required = db.Column(db.Boolean, default=False)
    priority = db.Column(db.String(20))  # e.g., 'high', 'medium', 'low'
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc),
                                   onupdate=datetime.now(timezone.utc))

    # Relationships
    question = db.relationship('TaxQuestion', backref=db.backref('document_mappings', lazy=True))
    document_type = db.relationship('DocumentType', backref=db.backref('question_mappings', lazy=True))

    def __repr__(self):
        return f'<QuestionDocumentMapping {self.id}: Q{self.question_id} -> Doc{self.document_type_id}>'

    def to_dict(self):
        """Convert the mapping to a dictionary"""
        return {
            'id': self.id,
            'question_id': self.question_id,
            'document_type_id': self.document_type_id,
            'response_trigger': self.response_trigger,
            'required': self.required,
            'priority': self.priority,
            'document_type': self.document_type.type_name if self.document_type else None
        }

    @staticmethod
    def get_required_documents(question_id, answer):
        """
        Get required documents based on question answer

        Args:
            question_id: ID of the question
            answer: User's answer to the question

        Returns:
            List of required document types
        """
        mappings = QuestionDocumentMapping.query.filter_by(
            question_id=question_id
        ).all()

        required_docs = []
        for mapping in mappings:
            # Check if answer matches the trigger
            if mapping.response_trigger.lower() == str(answer).lower():
                doc_type = DocumentType.query.get(mapping.document_type_id)
                if doc_type:
                    required_docs.append({
                        'document_type': doc_type.type_name,
                        'description': doc_type.description,
                        'required': mapping.required,
                        'priority': mapping.priority
                    })

        return required_docs


class CustomerResponse(db.Model):
    __tablename__ = 'customer_responses'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)
    tenant_id = db.Column(db.Integer, nullable=True)
    tax_year = db.Column(db.Integer, nullable=False)
    question_id = db.Column(db.Integer, nullable=False)
    response_value = db.Column(db.Text, nullable=True)
    additional_notes = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, nullable=False)
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer, nullable=False)
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc),
                                   onupdate=datetime.now(timezone.utc))

    # Relationships
    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('responses', lazy=True))
    customer = db.relationship('Customer', foreign_keys=[customer_id], backref=db.backref('responses', lazy=True))

    __table_args__ = (
        db.Index('idx_customer_responses_user_id', 'user_id'),
        db.CheckConstraint('created_date <= last_modified_date', name='check_dates'),
    )


class ActivityLog(db.Model):
    __tablename__ = 'activity_log'

    log_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False)
    user_name = db.Column(db.String(100), nullable=False)
    action = db.Column(db.String(255), nullable=False)
    document_id = db.Column(db.Integer, db.ForeignKey('documents.document_id'), nullable=True)
    file_name = db.Column(db.String(255), nullable=True)
    file_path = db.Column(db.String(512), nullable=True)
    status = db.Column(db.String(20), nullable=True)
    processing_time = db.Column(db.DateTime(timezone=True), nullable=True)
    timestamp = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))


class Ticket(db.Model):
    __tablename__ = 'tickets'

    id = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(20), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'))
    category_picklist = db.Column(
        db.Enum('Document', 'General', 'Technical', 'Account', 'Other',
                name='category_type'),
        nullable=False
    )
    status_picklist = db.Column(
        db.Enum('Open', 'Inprogress', 'Onhold', 'Closed',
                name='status_type'),
        default='Open'
    )
    subject = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    priority = db.Column(
        db.Enum('HIGH', 'MEDIUM', 'LOW', name='priority_type'),
        nullable=False
    )
    severity = db.Column(db.String(20))
    assigned_agent_id = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    parent_ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'))
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    first_response_date = db.Column(db.DateTime(timezone=True))
    resolved_date = db.Column(db.DateTime(timezone=True))
    closed_date = db.Column(db.DateTime(timezone=True))
    resolution_summary = db.Column(db.Text)

    # Relationships
    customer = db.relationship('Customer', backref='tickets')
    assigned_agent = db.relationship('User', foreign_keys=[assigned_agent_id])
    creator = db.relationship('User', foreign_keys=[created_by])
    responses = db.relationship('TicketResponse', backref='ticket', lazy='dynamic')
    __table_args__ = (
        db.Index('idx_ticket_customer', 'customer_id'),
        db.Index('idx_ticket_status', 'status_picklist'),
        db.Index('idx_ticket_created', 'created_date'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'ticket_number': self.ticket_number,
            'category': self.category_picklist,
            'status': self.status_picklist,
            'subject': self.subject,
            'description': self.description,
            'priority': self.priority,
            'created_date': self.created_date.isoformat() if self.created_date else None,
            'customer_name': f"{self.customer.first_name} {self.customer.last_name}" if self.customer else None
        }


class TicketResponse(db.Model):
    __tablename__ = 'ticket_responses'

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'))
    response_type = db.Column(
        db.Enum('AGENT_RESPONSE', 'CUSTOMER_RESPONSE', 'SYSTEM_NOTE',
                name='response_type'),
        nullable=False
    )
    response_text = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    is_internal = db.Column(db.Boolean, default=False)

    # Relationships
    attachments = db.relationship('ResponseAttachment', backref='response', lazy='dynamic')
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'id': self.id,
            'ticket_id': self.ticket_id,
            'response_type': self.response_type,
            'response_text': self.response_text,
            'created_by': self.created_by,
            'created_date': self.created_date.isoformat() if self.created_date else None,
            'is_internal': self.is_internal,
            'attachments': [attachment.to_dict() for attachment in self.attachments]
        }


class ResponseAttachment(db.Model):
    __tablename__ = 'response_attachments'

    id = db.Column(db.Integer, primary_key=True)
    response_id = db.Column(db.Integer, db.ForeignKey('ticket_responses.id'))
    file_name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(512), nullable=False)
    file_size = db.Column(db.LargeBinary)  # Changed to BYTEA
    mime_type = db.Column(db.String(100), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id,
            'response_id': self.response_id,
            'file_name': self.file_name,
            'mime_type': self.mime_type,
            'created_date': self.created_date.isoformat() if self.created_date else None
        }


class ResponseTemplate(db.Model):
    __tablename__ = 'response_templates'

    id = db.Column(db.Integer, primary_key=True)
    category_picklist = db.Column(
        db.Enum('Document', 'General', 'Technical', 'Account', 'Other',
                name='template_category_type')
    )
    name = db.Column(db.String(100), nullable=False)
    subject = db.Column(db.String(255))
    body = db.Column(db.Text, nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime(timezone=True), default=datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id,
            'category': self.category_picklist,
            'name': self.name,
            'subject': self.subject,
            'body': self.body,
            'active': self.active
        }


class NotificationTypes(db.Model):
    __tablename__ = 'notification_types'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    priority = db.Column(db.String(20), nullable=False)
    description = db.Column(db.Text)
    template_subject = db.Column(db.Text, nullable=False)
    template_body = db.Column(db.Text, nullable=False)
    frequency_type = db.Column(db.String(50))
    requires_acknowledgment = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), nullable=False)
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime(timezone=True), nullable=False)


class NotificationTriggers(db.Model):
    __tablename__ = 'notification_triggers'

    id = db.Column(db.Integer, primary_key=True)
    notification_type_id = db.Column(db.Integer, db.ForeignKey('notification_types.id'))
    trigger_type = db.Column(db.String(50), nullable=False)
    trigger_entity_type = db.Column(db.String(50))
    trigger_entity_id = db.Column(db.Integer)
    trigger_condition = db.Column(db.String(100))
    days_offset_start = db.Column(db.Integer)
    days_offset_end = db.Column(db.Integer)
    active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), nullable=False)
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime(timezone=True), nullable=False)


class CustomerNotifications(db.Model):
    __tablename__ = 'customer_notifications'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'))
    notification_type_id = db.Column(db.Integer, db.ForeignKey('notification_types.id'))
    document_id = db.Column(db.Integer, db.ForeignKey('documents.document_id'))
    subject = db.Column(db.Text, nullable=False)
    message = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(50), nullable=False)
    related_entity_type = db.Column(db.String(50))
    related_entity_id = db.Column(db.Integer)
    due_date = db.Column(db.DateTime(timezone=True))
    read_date = db.Column(db.DateTime(timezone=True))
    acknowledged_date = db.Column(db.DateTime(timezone=True))
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), nullable=False)
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime(timezone=True), nullable=False)

    # Relationships
    document = db.relationship('Document', backref='notifications')
    notification_type = db.relationship('NotificationTypes')
    customer = db.relationship('Customer')


class NotificationDeliveries(db.Model):
    __tablename__ = 'notification_deliveries'

    id = db.Column(db.Integer, primary_key=True)
    notification_id = db.Column(db.Integer, db.ForeignKey('customer_notifications.id'))
    response_id = db.Column(db.Integer, db.ForeignKey('ticket_responses.id'))
    channel = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(50), nullable=False)
    sent_date = db.Column(db.DateTime(timezone=True))
    error_message = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    created_date = db.Column(db.DateTime(timezone=True), nullable=False)
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    last_modified_date = db.Column(db.DateTime(timezone=True), nullable=False)
    # Relationship
    notification = db.relationship('CustomerNotifications', backref='deliveries')
    response = db.relationship('TicketResponse', backref='deliveries', foreign_keys=[response_id])  # New relationship


class CustomerNotificationItem(db.Model):
    __tablename__ = 'customer_notification_items'

    customer_notification_item_id = db.Column(db.Integer, primary_key=True)
    customer_notification_id = db.Column(db.Integer, db.ForeignKey('customer_notifications.id'), nullable=False)
    customer_id = db.Column(db.Integer, nullable=False)
    document_id = db.Column(db.Integer, db.ForeignKey('documents.document_id'), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False)
    created_date = db.Column(db.DateTime, nullable=False, default=datetime.now(timezone.utc))
    last_modified_by = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False)
    last_modified_date = db.Column(db.DateTime, nullable=False, default=datetime.now(timezone.utc),
                                   onupdate=datetime.now(timezone.utc))

    # Add relationships
    notification = db.relationship('CustomerNotifications', backref='notification_items')
    document = db.relationship('Document', backref='notification_items')

    def __repr__(self):
        return f'<CustomerNotificationItem {self.customer_notification_item_id}>'


class TaxFinancialYear(db.Model):
    __tablename__ = 'tax_financialyear'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False)
    tax_year = db.Column(db.Integer, nullable=False, unique=True)  # This is the uniquely constrained column
    status = db.Column(db.String(50), nullable=False)
    start_date = db.Column(db.DateTime, nullable=False)
    end_date = db.Column(db.DateTime, nullable=False)

    # Relationships using tax_year as the reference
    customer_tax_financials = db.relationship('CustomerTaxFinancial', backref='financial_year')
    joint_members = db.relationship('CustomerJointMember', backref='financial_year')


class CustomerTaxFinancial(db.Model):
    __tablename__ = 'customer_taxfinancial'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False)
    tax_financialyear = db.Column(db.Integer, db.ForeignKey('tax_financialyear.tax_year'),
                                  nullable=False)  # Reference tax_year
    customer_id = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(50), nullable=False)
    filing_type = db.Column(db.String(50), nullable=False)
    previous_filing_type = db.Column(db.String(50), nullable=True)
    previous_type_date = db.Column(db.DateTime, nullable=True)


class CustomerJointMember(db.Model):
    __tablename__ = 'customer_jointmembers'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False)
    tax_financialyear = db.Column(db.Integer, db.ForeignKey('tax_financialyear.tax_year'),
                                  nullable=False)  # Reference tax_year
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('dependents.id'), nullable=False)
    member_name = db.Column(db.String(100), nullable=False)

    # Relationships
    customer = db.relationship('Customer', backref='joint_members')
    member = db.relationship('Dependent', backref='joint_memberships')


def log_activity(user_id, user_name, action, document_id=None, file_name=None, file_path=None, status=None,
                 processing_time=None):
    new_log = ActivityLog(
        user_id=user_id,
        user_name=user_name,
        action=action,
        document_id=document_id,
        file_name=file_name,
        file_path=file_path,
        status=status,
        processing_time=processing_time
    )
    db.session.add(new_log)
    db.session.commit()


def log_document_error(document_id, error_message, user_id):
    try:
        # Log to activity log
        log_activity(
            user_id=user_id,
            user_name=User.query.get(user_id).username,
            action="Document Error",
            document_id=document_id,
            status='ERROR',
            file_name=None,
            processing_time=datetime.now(timezone.utc)
        )

        # Log to application logger
        print(f"Document {document_id} error: {error_message}")

    except Exception as e:
        print(f"Error logging document error: {str(e)}")


def create_initial_admin_if_not_exists():
    admin = User.query.filter_by(email='saikiran@mindlinksinc.com').first()
    support_agent = User.query.filter_by(user_role='support_agent').first()
    if not admin:
        email = config['INITIAL_ADMIN']['EMAIL']
        password = config['INITIAL_ADMIN']['PASSWORD']
        username = config['INITIAL_ADMIN']['USERNAME']
        hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
        new_admin = User(
            username=username,
            email=email,
            password_hash=hashed_password,
            is_declared=True,
            user_role='super_admin'
        )
        db.session.add(new_admin)
        db.session.commit()
        print("Initial admin user created with email: 'saikiran@mindlinksinc.com'")
    if not support_agent:
        hashed_password = generate_password_hash('kevindang', method='pbkdf2:sha256')
        new_agent = User(
            username='kevin dang',
            email='kevin@refractionalcfo.com',
            password_hash=hashed_password,
            is_declared=True,
            user_role='support_agent'
        )
        db.session.add(new_agent)
        db.session.commit()
        print("Initial agent user created with email: 'kevin@refractionalcfo.com'")


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):

        # Get Authorization header
        auth_header = request.headers.get('Authorization')

        if not auth_header:
            print("No Authorization header found")
            return jsonify({'message': 'Token is missing!'}), 401

        try:
            # Extract token from "Bearer <token>"
            if not auth_header.startswith('Bearer '):
                print("Authorization header does not start with 'Bearer'")
                return jsonify({'message': 'Invalid token format!'}), 401

            token = auth_header.split(' ')[1]
            print(f"Extracted token: {token[:10]}...")  # Log first 10 chars for security

            # Decode token
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            print(f"Decoded token data: {data}")

            # Get current user
            current_user = User.query.filter_by(user_id=data['user_id']).first()
            if not current_user:
                print(f"No user found with id: {data.get('user_id')}")
                return jsonify({'message': 'User not found!'}), 404

            print(f"Found user: {current_user.user_id}, role: {current_user.user_role}")

            # Call the decorated function with the authenticated user
            return f(current_user, *args, **kwargs)

        except jwt.ExpiredSignatureError:
            print("Token has expired")
            return jsonify({'message': 'Token has expired!'}), 401

        except jwt.InvalidTokenError as e:
            print(f"Invalid token error: {str(e)}")
            return jsonify({'message': f'Invalid token: {str(e)}'}), 401

        except Exception as e:
            print(f"Unexpected error in token validation: {str(e)}")
            return jsonify({'message': f'Token validation error: {str(e)}'}), 500

    return decorated


@app.route('/test-upload-folder', methods=['GET'])
def test_upload_folder():
    try:
        test_file_path = os.path.join(app.config['UPLOAD_FOLDER'], 'test.txt')
        with open(test_file_path, 'w') as f:
            f.write('test')
        os.remove(test_file_path)
        return jsonify({
            "status": "success",
            "message": "Upload folder is writable",
            "path": app.config['UPLOAD_FOLDER']
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
            "path": app.config['UPLOAD_FOLDER']
        })


@app.route('/api/check-token-validity', methods=['POST'])
def check_token_validity():
    """Debug endpoint to check token validity and user status"""
    data = request.form if request.form else request.json
    token = data.get('declaration_token')

    if not token:
        return jsonify({
            'valid': False,
            'message': 'No token provided'
        }), 400

    user = User.query.filter_by(declaration_token=token).first()

    if not user:
        return jsonify({
            'valid': False,
            'message': 'Invalid token - no matching user found'
        }), 401

    # Return detailed status for debugging
    return jsonify({
        'valid': True,
        'user_status': {
            'user_id': user.user_id,
            'email': user.email,
            'is_declared': user.is_declared,
            'status': user.status,
            'has_token': bool(user.declaration_token)
        }
    })


def declaration_token_required(f):
    """Decorator for validating the declaration token."""

    @wraps(f)
    def decorated(*args, **kwargs):
        # Log the request headers
        print(f"Headers: {dict(request.headers)}")
        print(f"Form Data: {dict(request.form)}")

        # Extract token from Authorization header
        auth_header = request.headers.get('Authorization')
        token = None
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]  # Extract token after 'Bearer '

        # If not found in Authorization, fall back to other locations
        if not token:
            token = (
                    request.form.get('declaration_token') or
                    (request.json.get('declaration_token') if request.is_json else None) or
                    request.args.get('declaration_token')
            )

        if not token:
            print("No declaration token found in the request")
            return jsonify({'error': 'Declaration token is required'}), 401

        try:
            # Validate and find the user associated with the token
            user = User.query.filter_by(declaration_token=token).first()

            if not user:
                print(f"No user found with token: {token}")
                return jsonify({'error': 'Invalid or expired declaration token'}), 401

            # Log user information if found
            print(f"User found: ID: {user.user_id}, Email: {user.email}")

            # Pass the user to the wrapped function
            return f(user, *args, **kwargs)

        except Exception as e:
            print(f"Error during token validation: {str(e)}")
            return jsonify({'error': f'Token validation error: {str(e)}'}), 500

    return decorated


@app.route('/debug/token-check', methods=['POST'])
def debug_token_check():
    """Debug endpoint to check token presence in request"""
    debug_info = {
        'headers': dict(request.headers),
        'form_data': dict(request.form),
        'args': dict(request.args),
        'is_json': request.is_json,
        'json_data': request.json if request.is_json else None,
        'found_token': None
    }

    # Check all possible places for token
    if 'declaration_token' in request.form:
        debug_info['found_token'] = request.form.get('declaration_token')
        debug_info['token_location'] = 'form_data'
    elif request.is_json and 'declaration_token' in request.json:
        debug_info['found_token'] = request.json.get('declaration_token')
        debug_info['token_location'] = 'json'
    elif 'declaration_token' in request.args:
        debug_info['found_token'] = request.args.get('declaration_token')
        debug_info['token_location'] = 'query_params'

    return jsonify(debug_info)


@app.route('/debug/user-check/<token>')
def debug_user_check(token):
    """Debug endpoint to check user status for a given token"""
    user = User.query.filter_by(declaration_token=token).first()
    if user:
        return jsonify({
            'user_found': True,
            'user_id': user.user_id,
            'is_declared': user.is_declared,
            'status': user.status,
            'email': user.email
        })
    return jsonify({
        'user_found': False,
        'message': 'No user found with this token'
    })



@app.route('/login', methods=['POST'])
def login():
    auth = request.json
    if not auth or not auth.get('email') or not auth.get('password'):
        return jsonify({'message': 'Could not verify'}), 401

    user = User.query.filter_by(email=auth.get('email')).first()
    if not user:
        return jsonify({'message': 'User not found'}), 401
    if user.status == 'inactive' and (user.user_role == 'support_agent' or user.user_role == 'customer'):
        return jsonify({
            'message': 'Your account is currently inactive. Please contact an administrator.'
        }), 403

        # Add customer check
    if user.status == 'pending' and (user.user_role == 'support_agent' or user.user_role == 'customer'):
        return jsonify({
            'message': 'Your account not Yet Declared, Please Complete the Declaration Process'
        }), 403
    # Debug information
    print(f"""
    Login attempt:
    - User ID: {user.user_id}
    - Email: {user.email}
    - Role: {user.user_role}
    """)

    # Get customer record
    customer = Customer.query.filter_by(user_id=user.user_id).first()
    try:
        customer_taxfinancial = CustomerTaxFinancial.query.filter_by(customer_id=customer.id).first()
        print("customer_taxfinancial:", customer_taxfinancial, "filing type:", customer_taxfinancial.filing_type)
        if customer_taxfinancial.filing_type == 'Married filing jointly':
            customer_jointmembers = CustomerJointMember.query.filter_by(customer_id=customer.id).first()
            spouse_name = customer_jointmembers.member_name
            print("Spouse Name:", spouse_name)
        else:
            spouse_name = None
    except Exception as e:
        print("exception:", e)
    if customer:
        print(f"""
        Customer found:
        - Customer ID: {customer.id}
        - Documents count: {Document.query.filter_by(customer_id=customer.id).count()}
        """)
    else:
        print(f"No customer record found for user_id: {user.user_id}")

    if check_password_hash(user.password_hash, auth.get('password')):
        token = jwt.encode({
            'user_id': user.user_id,
            'user_role': user.user_role,
            'exp': datetime.now(timezone.utc) + timedelta(hours=24)
        }, app.config['SECRET_KEY'], algorithm="HS256")

        print(f"Login successful for user: {user.user_id}")

        response_data = {
            'token': token,
            'user_id': user.user_id,
            'username': user.username,
            'email': user.email,
            'user_role': user.user_role
        }

        # Add customer details if the user is a customer
        if user.user_role == 'customer' and customer:
            response_data['customer'] = {
                'customer_id': customer.id,
                'filing_type': customer_taxfinancial.filing_type,
                'spouse_name': spouse_name
            }

        return jsonify(response_data), 200

    return jsonify({'message': 'Invalid credentials'}), 401

@app.errorhandler(Exception)
def handle_error(error):
    print(f'Error: {str(error)}')
    return jsonify({
        'error': str(error),
        'status': 'error'
    }), 500


# @app.route('/login', methods=['POST'])
# def login():
#     auth = request.json
#     if not auth or not auth.get('email') or not auth.get('password'):
#         return jsonify({'message': 'Could not verify'}), 401
#
#     user = User.query.filter_by(email=auth.get('email')).first()
#     if not user:
#         return jsonify({'message': 'User not found'}), 401
#     if user.status == 'inactive' and user.user_role == 'support_agent':
#         return jsonify({
#             'message': 'Your account is currently inactive. Please contact an administrator.'
#         }), 403
#
#         # Add customer check
#     if user.status == 'inactive' and user.user_role == 'customer':
#         return jsonify({
#             'message': 'Your account is currently inactive. Please contact an administrator.'
#         }), 403
#     # Debug information
#     print(f"""
#     Login attempt:
#     - User ID: {user.user_id}
#     - Email: {user.email}
#     - Role: {user.user_role}
#     """)
#
#     # Get customer record
#     customer = Customer.query.filter_by(user_id=user.user_id).first()
#     if customer:
#         print(f"""
#         Customer found:
#         - Customer ID: {customer.id}
#         - Documents count: {Document.query.filter_by(customer_id=customer.id).count()}
#         """)
#     else:
#         print(f"No customer record found for user_id: {user.user_id}")
#
#     if check_password_hash(user.password_hash, auth.get('password')):
#         token = jwt.encode({
#             'user_id': user.user_id,
#             'user_role': user.user_role,
#             'exp': datetime.now(timezone.utc) + timedelta(hours=24)
#         }, app.config['SECRET_KEY'], algorithm="HS256")
#
#         print(f"Login successful for user: {user.user_id}")
#
#         return jsonify({
#             'token': token,
#             'user_id': user.user_id,
#             'username': user.username,
#             'email': user.email,
#             'user_role': user.user_role
#         }), 200
#
#     return jsonify({'message': 'Invalid credentials'}), 401


@app.route('/declaration_login', methods=['POST'])
def declaration_login():
    try:
        data = request.get_json()
        email = data.get('email')
        password = data.get('password')

        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({'message': 'User not found'}), 401

        if check_password_hash(user.password_hash, password):
            token = jwt.encode({
                'user_id': user.user_id,
                'user_role': user.user_role,
                'exp': datetime.now(timezone.utc) + timedelta(hours=24)
            }, app.config['SECRET_KEY'])

            return jsonify({
                'token': token,
                'user_id': user.user_id,
                'username': user.username,
                'email': user.email,
                'user_role': user.user_role
            }), 200

        return jsonify({'message': 'Invalid credentials'}), 401

    except Exception as e:
        return jsonify({'message': str(e)}), 500


@app.route('/categorized_documents', methods=['GET'])
@token_required
def get_categorized_documents(current_user):
    try:
        print(f"Inside categorized documents for user_id: {current_user.user_id}")

        # First get the customer record for this user
        customer = Customer.query.filter_by(user_id=current_user.user_id).first()
        if not customer:
            print(f"No customer record found for user_id: {current_user.user_id}")
            return jsonify({
                'categories': {},
                'summary': {
                    'total_documents': 0,
                    'total_categories': 0
                }
            })

        print(f"Found customer with ID: {customer.id}")

        # Create category mapping
        categories = defaultdict(lambda: {
            'name': '',
            'documents': []
        })
        print("customer:", customer, "\ncurrent_user:", current_user)
        # Get documents for the customer with their document types
        documents = (
            db.session.query(Document, DocumentType)
            .join(DocumentType, Document.document_type_id == DocumentType.document_type_id)
            .filter(Document.customer_id == customer.id)
            .filter(DocumentType.document_type_id != 48)
            .filter(Document.is_deleted == False)
            .all()
        )

        print(f"Found {len(documents)} documents for customer")

        # Process documents and organize by category
        for doc, doc_type in documents:
            if doc_type:
                category_name = doc_type.category_name.strip() if doc_type.category_name else "Other Documents"
                categories[category_name]['name'] = category_name

                doc_info = {
                    'document_id': doc.document_id,
                    'document_type_id': doc_type.document_type_id,  # Add this line
                    'type_name': doc_type.type_name,  # Use type_name from DocumentType
                    'description': doc_type.description,
                    'status': doc.status.strip() if doc.status else 'PENDING',
                    'file_name': doc.file_name,
                    'due_date': doc.due_date.isoformat() if doc.due_date else None,
                    'priority': doc.priority,
                    'upload_date': doc.upload_date.isoformat() if doc.upload_date else None,
                    'process_status': doc.process_status,
                    'submitted_documents': 1 if doc.status == 'SUBMITTED' else 0,
                    'total_documents': 1,
                    'customer_entered_due_date': doc.customer_entered_due_date
                }

                categories[category_name]['documents'].append(doc_info)
        for x in categories:
            print("Categories[",x,"]:", categories[x])
        return jsonify({
            'categories': categories,
            'summary': {
                'total_documents': len(documents),
                'total_categories': len(categories)
            }
        })

    except Exception as e:
        print(f"Error in get_categorized_documents: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/uploaded_documents/<int:document_id>', methods=['GET'])
@token_required
def view_document(current_user, document_id):
    try:
        print(f"Fetching document {document_id} for user {current_user.user_id}")

        # Get the initial document
        initial_document = Document.query.get(document_id)
        if not initial_document:
            return jsonify({"error": "Document not found"}), 404

        # Get customer record
        customer = Customer.query.filter_by(user_id=current_user.user_id).first()
        if not customer:
            return jsonify({"error": "Customer record not found"}), 404

        # Get all related documents by document_type_id for this customer
        related_documents = Document.query.filter(
            Document.customer_id == customer.id,
            Document.document_type_id == initial_document.document_type_id,
            Document.tax_year == initial_document.tax_year
        ).order_by(Document.created_at.asc()).all()

        # Get document type info
        doc_type = DocumentType.query.get(initial_document.document_type_id)

        response_data = {
            "document_info": {
                "type_name": doc_type.type_name,
                "description": doc_type.description,
                "total_slots": len(related_documents)
            },
            "documents": []
        }

        # Process each document
        for idx, doc in enumerate(related_documents):
            document_data = {
                "document_id": doc.document_id,
                "slot_number": idx + 1,  # Start from 1
                "file_name": doc.file_name,
                "status": doc.status.strip() if doc.status else 'PENDING',
                "upload_date": doc.upload_date.isoformat() if doc.upload_date else None,
                "preview_url": f"/api/documents/{doc.document_id}/preview" if doc.status == 'SUBMITTED' else None
            }
            response_data["documents"].append(document_data)

        return jsonify(response_data), 200

    except Exception as e:
        print(f"Error viewing document: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/agent_view_pending_documents/<int:customer_id>', methods=['GET'])
@token_required
def get_customer_pending_documents(current_user, customer_id):
    try:
        # Get the customer
        customer = Customer.query.get(customer_id)
        if not customer:
            return jsonify({"error": "Customer not found"}), 404

        # Get all pending documents for the customer with their types
        pending_documents = (db.session.query(Document, DocumentType)
                             .join(DocumentType, Document.document_type_id == DocumentType.document_type_id)
                             .filter(
            Document.customer_id == customer.id,
            Document.status != 'SUBMITTED',
            Document.document_type_id != 48  # Filter for pending documents
        ).all())

        response_data = {
            "document_info": {
                "customer_name": f"{customer.first_name} {customer.last_name}" if hasattr(customer,
                                                                                          'first_name') else "Unknown",
                "total_documents": len(pending_documents)
            },
            "documents": []
        }

        # Process each document
        for doc, doc_type in pending_documents:
            document_data = {
                "document_id": doc.document_id,
                "type_name": doc_type.type_name,
                "file_name": doc.file_name,
                "status": doc.status.strip() if doc.status else 'Pending',
                "due_date": doc.due_date.isoformat() if doc.due_date else None
            }
            response_data["documents"].append(document_data)

        return jsonify(response_data), 200

    except Exception as e:
        print(f"Error getting pending documents: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/agent_view_document/<int:customer_id>', methods=['GET'])
@token_required
def get_customer_documents(current_user, customer_id):
    try:
        # Get the customer
        customer = Customer.query.get(customer_id)
        if not customer:
            return jsonify({"error": "Customer not found"}), 404

        # Get all documents for the customer with their types
        documents = (db.session.query(Document, DocumentType)
                     .join(DocumentType, Document.document_type_id == DocumentType.document_type_id)
                     .filter(Document.customer_id == customer.id)
                     .all())

        response_data = {
            "document_info": {
                "customer_name": f"{customer.first_name} {customer.last_name}" if hasattr(customer,
                                                                                          'first_name') else "Unknown",
                "total_documents": len(documents)
            },
            "documents": []
        }

        # Process each document
        for doc, doc_type in documents:
            document_data = {
                "document_id": doc.document_id,
                "type_name": doc_type.type_name,
                "file_name": doc.file_name,
                "status": doc.status.strip() if doc.status else 'PENDING',
                "upload_date": doc.upload_date.isoformat() if doc.upload_date else None,
                "due_date": doc.due_date.isoformat() if doc.due_date else None,
                "preview_url": f"/api/documents/{doc.document_id}/preview" if doc.status == 'SUBMITTED' else None
            }
            response_data["documents"].append(document_data)

        return jsonify(response_data), 200

    except Exception as e:
        print(f"Error getting customer documents: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/create_customer', methods=['POST'])
@token_required
def create_customer(current_user):
    if not current_user.is_admin:
        return jsonify({"message": "Admin privilege required"}), 403

    data = request.json
    email = data.get('email')
    first_name = data.get('first_name')
    last_name = data.get('last_name')

    # Check for required fields
    if not email or not first_name or not last_name:
        return jsonify({"message": "Email, first name, and last name are required"}), 400

    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        return jsonify({"message": "User with this email already exists"}), 409

    # Generate a unique token for the declaration link
    declaration_token = str(uuid.uuid4())
    print("Generated declaration token:", declaration_token)
    username = first_name+" "+last_name

    # Create new user in the User table
    new_user = User(
        email=email,
        username=username,
        user_role='customer',
        status='pending',
        declaration_token=declaration_token,  # Setting the token
        is_declared=False,
        password_hash='',  # Set an empty string for now
        first_name=first_name,
        last_name=last_name,
        tenant_id=1
    )
    db.session.add(new_user)
    db.session.flush()  # This assigns an ID to new_user without committing
    print(f"New user ID after flush: {new_user.user_id}")  # Debugging statement
    print(f"Stored declaration token in User table: {new_user.declaration_token}")  # Verify token is set

    # Create a corresponding entry in the Customers table
    new_customer = Customer(
        user_id=new_user.user_id,  # new_user.user_id is available after flush
        tenant_id=current_user.tenant_id,  # Adjust as needed
        created_by=current_user.user_id,
        created_date=datetime.now(timezone.utc),  # Explicitly set only created_date
        last_modified_by=current_user.user_id  # No need to set last_modified_date
    )
    db.session.add(new_customer)

    try:
        db.session.commit()  # Committing all changes
        print("Transaction committed successfully. User and Customer created.")

        # Generate the frontend URL for the declaration page with email as a parameter
        declaration_link = f"{app.config['FRONTEND_URL']}/declaration/{declaration_token}?email={email}"

        # Send email with the declaration link
        email_sent = send_declaration_email(email, declaration_link)
        response_message = "New customer created successfully"
        if not email_sent:
            response_message += " (Warning: Email delivery failed)"
        return jsonify({
            "message": response_message,
            "declaration_link": declaration_link,
            "user_email": email
        }), 201
    except Exception as e:
        db.session.rollback()
        print(f"Error creating customer: {str(e)}")
        return jsonify({"message": f"Error creating customer: {str(e)}"}), 500


def send_declaration_email(email, declaration_link):
    try:
        # Create HTML email message
        templates = load_email_templates()
        template = templates['DECLARATION']

        # Format the HTML content with the declaration link and current year
        html_content = template['HTML'].format(
            declaration_link=declaration_link,
            current_year=datetime.now().year
        )

        # Create email message with HTML content
        msg = Message(
            subject=template['SUBJECT'],
            recipients=[email],
            html=html_content
        )

        # Send email
        mail.send(msg)

        # Log success
        print(f"""
        =====================================
        Email sent successfully to: {email}
        Declaration Link: {declaration_link}
        =====================================
        """)

        return True

    except Exception as e:
        # Log error
        print(f"Failed to send email to {email}: {str(e)}")
        return False


@app.route('/api/validate-token/<token>', methods=['GET'])
def validate_token(token):
    user = User.query.filter_by(declaration_token=token).first()
    if not user:
        return jsonify({"valid": False, "message": "Invalid token"}), 404
    if user.is_declared:
        return jsonify({"valid": False, "message": "Token already used"}), 400

    return jsonify({
        "valid": True,
        "user": {
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name
        }
    }), 200


@app.route('/api/declaration/<token>', methods=['GET'])
def declaration_page(token):
    user = User.query.filter_by(declaration_token=token).first()
    if not user:
        return jsonify({"error": "Invalid or expired declaration link"}), 404
    if user.is_declared:
        return jsonify({"error": "User has already declared"}), 400

    # Add status check and password info
    return jsonify({
        "user_id": user.user_id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "is_declared": user.is_declared,
        "has_password": bool(user.password_hash),
        "current_step": "document_upload" if user.password_hash else "set_password"
    }), 200


@app.route('/set_password', methods=['POST'])
def set_password():
    data = request.json
    token = data.get('token')
    password = data.get('password')

    if not token or not password:
        return jsonify({"message": "Token and password are required"}), 400

    user = User.query.filter_by(declaration_token=token).first()
    if not user:
        return jsonify({"message": "Invalid or expired declaration link"}), 404
    if user.is_declared:
        return jsonify({"message": "User has already declared"}), 400

    try:
        # Always allow password update until declaration is complete
        hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
        user.password_hash = hashed_password
        user.status = 'pending'  # Keep status as pending until declaration is complete
        db.session.commit()
        return jsonify({"message": "Password set successfully"}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Error setting password: {str(e)}"}), 500


# Add this endpoint to serve document files
@app.route('/get_document/<int:document_id>', methods=['GET'])
@token_required
def get_document(current_user, document_id):
    document = Document.query.get(document_id)
    if not document or document.customer_id != current_user.user_id:
        return jsonify({"error": "Document not found or unauthorized"}), 404

    try:
        return send_file(
            document.file_path,
            mimetype=document.mime_type,
            as_attachment=False,
            download_name=document.file_name
        )
    except Exception as e:
        return jsonify({"error": "Failed to retrieve document"}), 500


@app.route('/upload_declaration_document', methods=['POST'])
@declaration_token_required
def upload_declaration_document(current_user):
    """Handle document upload during the declaration process."""
    print(f"Starting document upload process for user {current_user.user_id}")

    try:
        # Check if the file is in the request
        if 'file' not in request.files:
            print("No file part in the request")
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files['file']

        # Check if the file has a valid filename
        if file.filename == '':
            print("No file selected")
            return jsonify({"error": "No file selected"}), 400

        # Ensure the file is a PDF
        if not file.filename.lower().endswith('.pdf'):
            print("Invalid file type, only PDF allowed")
            return jsonify({"error": "Please upload a PDF file"}), 400

        # Proceed with file saving and processing
        try:
            customer = db.session.query(Customer).join(
                User, Customer.user_id == User.user_id
            ).filter(
                User.user_id == current_user.user_id
            ).first()

            if not customer:
                print(f"No customer record found for user_id {current_user.user_id}")
                return jsonify({"error": "Customer record not found"}), 404
            # Save the uploaded file
            filename = secure_filename(f"declaration_{current_user.user_id}_{int(time.time())}")
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            file.save(file_path)

            # Find the document type (Form 1040EZ)
            doc_type = DocumentType.query.filter_by(type_name='Form 1040').first()
            if not doc_type:
                raise Exception("Document type 'Form 1040' not found")
            print("Customer:", customer)
            # Create a document record in the database
            document = Document(
                customer_id=customer.id,
                document_type_id=doc_type.document_type_id,
                file_name=filename,
                file_path=file_path,
                file_size=os.path.getsize(file_path),
                mime_type='application/pdf',
                process_status='UPLOADED',
                status='PENDING_VERIFICATION',
                tax_year=datetime.now().year,
                requirement_source='Form 1040',
                tenant_id=1,
                created_by=current_user.user_id,
                last_modified_by=current_user.user_id,
                upload_date=datetime.now(),
                is_deleted=False
            )

            db.session.add(document)
            db.session.commit()

            # Process the document (OCR and data extraction)
            ocr_result = perform_ocr(file_path)
            print("OCR Result Status:", ocr_result['ocr_status'])
            print("OCR RESULT:", ocr_result)
            if not ocr_result['ocr_status']:
                document.status = 'ERROR'
                db.session.commit()

                # Check for password protected error
                if ocr_result.get('error_type') == 'password_protected':
                    print("Returning the password-protected error message")
                    return jsonify({
                        'status': 'error',
                        'message': ocr_result['error_message'],
                        'error_type': 'password_protected',
                        'validation_result': False,
                        'validation_message': 'Password protected PDF files are not supported'
                    }), 400

                print("Returning the general error message")
                # Handle other OCR errors
                return jsonify({
                    'status': 'error',
                    'message': ocr_result.get('error_message', 'OCR processing failed'),
                    'error_type': ocr_result.get('error_type', 'general_error'),
                    'validation_result': False,
                    'validation_message': 'Unable to extract text from the file'
                }), 400

            try:
                ocr_filename = f"{document.document_id}_ocr.txt"
                ocr_path = os.path.join(app.config['OCR_FOLDER'], ocr_filename)
                with open(ocr_path, 'w', encoding='utf-8') as f:
                    f.write(ocr_result['ocr_result'])

                # Save OCR result to database
                ocr_entry = OCRResult(
                    document_id=document.document_id,
                    raw_text=ocr_result['ocr_result'],
                    confidence_score=ocr_result.get('confidence_score', 0),
                    processing_time=int(ocr_result.get('processing_time', 0)),
                    ocr_engine="Azure Form Recognizer",
                    status='SUCCESS' if ocr_result['ocr_status'] else 'FAILURE'
                )
                db.session.add(ocr_entry)
                db.session.commit()

            except Exception as e:
                print(f"Error processing OCR document {document.document_id}: {str(e)}")
                document.status = 'ERROR'
                db.session.commit()
                return {'status': 'error', 'document_id': document.document_id, 'error': str(e)}
            if ocr_result['ocr_status']:
                extracted_data = extract_data(ocr_result['ocr_result'], Form1040_prompt)
                print("CLAUDE raw response:", extracted_data['raw_response'])
                # Save extracted data
                raw_claude_filename = f"{document.document_id}_claude_raw.txt"
                raw_claude_path = os.path.join(app.config['EXTRACTED_CLAUDE'], raw_claude_filename)
                with open(raw_claude_path, 'w', encoding='utf-8') as f:
                    f.write(extracted_data['raw_response'])
                print("-----------------------------------------------------")
            else:
                return jsonify({"error": "Unable to Extract Text From the file, Try Again later"})
            parsed_Form1040_data = parseForm1040(extracted_data['raw_response'])
            print("CLAUDE parsed data:", parsed_Form1040_data)

            # Save parsed data to EXTRACTED_PARSED
            parsed_filename = f"{document.document_id}_parsed.json"
            parsed_path = os.path.join(app.config['EXTRACTED_PARSED'], parsed_filename)
            with open(parsed_path, 'w', encoding='utf-8') as f:
                json.dump(parsed_Form1040_data, f, ensure_ascii=False, indent=4)
            try:
                # Create entries for form details
                for key, value in parsed_Form1040_data['form_details'].items():
                    extracted_data_entry = ExtractedData(
                        document_id=document.document_id,
                        field_name=f"Form Details - {key}",
                        field_value=str(value),
                        confidence_score=1.0,  # Default confidence score
                        status='SUCCESS'
                    )
                    db.session.add(extracted_data_entry)

                # Create entries for personal information
                for category, info in parsed_Form1040_data['personal_information'].items():
                    if isinstance(info, dict):
                        if 'address' in info:  # Handle nested address
                            address_str = f"{info['address']['street']}, {info['address']['city']}, {info['address']['state']} {info['address']['zip']}"
                            extracted_data_entry = ExtractedData(
                                document_id=document.document_id,
                                field_name=f"Personal Information - {category} - Address",
                                field_value=address_str,
                                confidence_score=1.0,
                                status='SUCCESS'
                            )
                            db.session.add(extracted_data_entry)

                            # Add other fields except address
                            for key, value in info.items():
                                if key != 'address':
                                    extracted_data_entry = ExtractedData(
                                        document_id=document.document_id,
                                        field_name=f"Personal Information - {category} - {key}",
                                        field_value=str(value),
                                        confidence_score=1.0,
                                        status='SUCCESS'
                                    )
                                    db.session.add(extracted_data_entry)
                    else:
                        extracted_data_entry = ExtractedData(
                            document_id=document.document_id,
                            field_name=f"Personal Information - {category}",
                            field_value=str(info),
                            confidence_score=1.0,
                            status='SUCCESS'
                        )
                        db.session.add(extracted_data_entry)

                # Create entries for dependents
                for i, dependent in enumerate(parsed_Form1040_data['dependents'], 1):
                    for key, value in dependent.items():
                        extracted_data_entry = ExtractedData(
                            document_id=document.document_id,
                            field_name=f"Dependent {i} - {key}",
                            field_value=str(value),
                            confidence_score=1.0,
                            status='SUCCESS'
                        )
                        db.session.add(extracted_data_entry)

                # Create entries for required documents
                for i, doc in enumerate(parsed_Form1040_data['required_documents'], 1):
                    for key, value in doc.items():
                        extracted_data_entry = ExtractedData(
                            document_id=document.document_id,
                            field_name=f"Required Document {i} - {key}",
                            field_value=str(value),
                            confidence_score=1.0,
                            status='SUCCESS'
                        )
                        db.session.add(extracted_data_entry)
                db.session.commit()
            except Exception as e:
                print(f"Error processing parsed data document {document.document_id}: {str(e)}")
                document.status = 'ERROR'
                db.session.commit()
                return {'status': 'error', 'document_id': document.document_id, 'error': str(e)}
                # Validate the parsed Form 1040 data
                # Extract relevant user data from the current_user object and pass it as a dictionary
            try:
                print("calling validate_form")
                is_valid, validation_message = validate_form1040_response(parsed_Form1040_data, {
                    'first_name': current_user.first_name,
                    'last_name': current_user.last_name
                })
            except Exception as e:
                print("Exception:",e)
            # If validation is successful, return success
            if is_valid:
                document.process_status = 'COMPLETED'
                document.status = 'SUCCESS'
                db.session.commit()
                return jsonify({
                    "message": "Document uploaded and validated successfully" if is_valid else "Validation failed",
                    "document_id": document.document_id,
                    "extracted_data": parsed_Form1040_data,
                    "validationResult": {
                        "isValid": is_valid,
                        "message": validation_message
                    }
                }), 200
            else:
                return jsonify({
                    "message": "Document failed to validate, Validation failed",
                    "document_id": document.document_id,
                    "extracted_data": parsed_Form1040_data,
                    "validationResult": {
                        "isValid": is_valid,
                        "message": validation_message
                    }
                }), 200
        except Exception as e:
            print(f"Error processing document", e)
            # if os.path.exists(file_path):
            #     os.remove(file_path)  # Remove the file in case of failure
            return jsonify({"error": f"Error processing document"}), 500

    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return jsonify({"error": "An unexpected error occurred"}), 500


@app.route('/complete_declaration', methods=['POST'])
def complete_declaration():
    try:
        print("Starting complete_declaration endpoint")
        data = request.json
        print(f"Received data: {data}")

        # Validate incoming data
        if not data:
            print("No JSON data received")
            return jsonify({"error": "No data provided"}), 400

        token = data.get('token')
        extracted_data = data.get('extracted_data')
        if data.get('filing_status_change') != None:
            new_filing_status = data.get('filing_status_change', {}).get('newStatus')
            original_filing_status = data.get('filing_status_change', {}).get('originalStatus')
        else:
            print("AAAAAAAA::", extracted_data)
            new_filing_status = original_filing_status = extracted_data.get('personal_information', {}).get(
                'filing_status')
        print("FILING STATUS::", new_filing_status, original_filing_status)
        print(f"Token: {token}")
        print(f"Extracted data: {extracted_data}")

        # Validate required fields
        if not token or not extracted_data or 'required_documents' not in extracted_data:
            print("Missing required fields in request")
            return jsonify({
                "error": "Missing required data",
                "details": {
                    "token": bool(token),
                    "extracted_data": bool(extracted_data),
                    "required_documents": bool(extracted_data and 'required_documents' in extracted_data)
                }
            }), 400

        # Find user by declaration token
        user = User.query.filter_by(declaration_token=token).first()
        if not user:
            print(f"No user found for token: {token}")
            return jsonify({"error": "Invalid token"}), 400

        # Retrieve customer
        customer = Customer.query.filter_by(user_id=user.user_id).first()
        if not customer:
            print(f"No customer found for user_id: {user.user_id}")
            return jsonify({"error": "Customer not found for the user"}), 400

        # Get current year for due date
        current_year = datetime.now(timezone.utc).year
        year_end_due_date = date(current_year, 12, 31)

        try:
            with db.session.no_autoflush:
                print("Starting transaction")

                # Update user status
                user.is_declared = True
                user.status = 'active'

                # Update customer information from extracted data
                personal_info = extracted_data.get('personal_information', {})
                taxpayer_info = personal_info.get('taxpayer', {})
                address_info = taxpayer_info.get('address', {})

                # Update customer details
                customer.marital_status = personal_info.get('filing_status')
                customer.address = address_info.get('street')
                customer.city = address_info.get('city')
                customer.zip_code = address_info.get('zip')

                # Get state_id based on state abbreviation
                state_abbrev = address_info.get('state')
                if state_abbrev:
                    state = State.query.filter_by(abbreviation=state_abbrev).first()
                    if state:
                        customer.state_id = state.id

                customer.last_modified_by = user.user_id
                customer.last_modified_date = datetime.now(timezone.utc)

                # Get the tax year from form_details
                tax_year = int(extracted_data.get('form_details', {}).get('filing_year', datetime.now().year))
                print(f"Looking up tax financial year for: {tax_year}")

                # Get tax financial year record from reference table
                tax_financial_year = TaxFinancialYear.query.filter_by(
                    tenant_id=user.tenant_id,
                    tax_year=tax_year,
                    status='active'
                ).first()

                if not tax_financial_year:
                    print(f"Tax financial year not found for year: {tax_year}")
                    return jsonify({
                        "error": f"Tax financial year {tax_year} not found or not active",
                        "details": "Please ensure the tax year is properly configured in the system"
                    }), 400

                print(f"Found tax financial year: {tax_financial_year.tax_year}")

                # Replace the section starting at "Create CustomerTaxFinancial entry" until before "Process dependents"

                # Create CustomerTaxFinancial entry for all filing types
                print(f"Tax Year: {tax_year}")
                print(f"New Filing Status: {new_filing_status}")
                print(f"Original Filing Status: {original_filing_status}")

                questionnaire_data = data.get('questionnaire_data', {})
                filing_status_change = data.get('filing_status_change')
                spouse_info = None

                # Create tax financial record based on filing status change
                if new_filing_status and original_filing_status and new_filing_status != original_filing_status:
                    print("Creating records for the new filing status")
                    new_tax_financial = CustomerTaxFinancial(
                        tenant_id=user.tenant_id,
                        tax_financialyear=tax_year,
                        customer_id=customer.id,
                        status='active',
                        filing_type=new_filing_status,
                        previous_filing_type=original_filing_status,
                        previous_type_date=datetime.now()
                    )
                    db.session.add(new_tax_financial)
                    print(f"Added record for new filing status: {new_filing_status}")
                else:
                    print("Creating single record with current filing status")
                    tax_financial = CustomerTaxFinancial(
                        tenant_id=user.tenant_id,
                        tax_financialyear=tax_year,
                        customer_id=customer.id,
                        status='active',
                        filing_type=new_filing_status or original_filing_status
                    )
                    db.session.add(tax_financial)
                    print(f"Added record with filing status: {new_filing_status or original_filing_status}")

                # Handle spouse information based on filing status
                if filing_status_change:
                    if filing_status_change.get('originalStatus') == 'Single' and \
                            filing_status_change.get('newStatus') in ['Married filing jointly',
                                                                      'Married filing separately']:
                        # Get spouse info from questionnaire for status change from Single to MFJ/MFS
                        follow_up_answers = questionnaire_data.get('answers', {})
                        if '3' in follow_up_answers and follow_up_answers['3'] == 'yes':
                            spouse_details = questionnaire_data.get('followUpAnswers', {}).get('3', {})
                            spouse_info = {
                                'name': f"{spouse_details.get('spouseFirstName', '')} {spouse_details.get('spouseLastName', '')}".strip(),
                                'ssn': spouse_details.get('spouseSSN')
                            }
                            print(f"Got spouse info from questionnaire: {spouse_info['name']}")
                    elif filing_status_change.get('originalStatus') == 'Married filing jointly' and \
                            filing_status_change.get('newStatus') == 'Married filing jointly':
                        # Both previous and current status is MFJ - get from extracted data
                        spouse_info = extracted_data.get('personal_information', {}).get('spouse')
                        print("Got spouse info from extracted data for continuing MFJ status")
                    elif filing_status_change.get('newStatus') in ['Married filing jointly',
                                                                   'Married filing separately']:
                        # Any other change to MFJ/MFS - get from extracted data
                        spouse_info = extracted_data.get('personal_information', {}).get('spouse')
                        print("Got spouse info from extracted data for other MFJ/MFS status change")
                else:
                    # No filing status change - use extracted data for MFJ/MFS
                    filing_status = personal_info.get('filing_status')
                    if filing_status in ['Married filing jointly', 'Married filing separately']:
                        spouse_info = extracted_data.get('personal_information', {}).get('spouse')
                        print(f"Got spouse info from extracted data for unchanged {filing_status}")

                # Process spouse information if available
                if spouse_info and spouse_info.get('name'):
                    spouse_name = spouse_info['name'].strip()
                    if spouse_name:
                        print(f"Processing spouse information for: {spouse_name}")

                        # Always create/update dependent record for spouse
                        spouse_dependent = Dependent.query.filter_by(
                            customer_id=customer.id,
                            name=spouse_name
                        ).first()

                        if not spouse_dependent:
                            spouse_dependent = Dependent(
                                customer_id=customer.id,
                                name=spouse_name,
                                relationship='Spouse',
                                created_by=user.user_id,
                                created_date=datetime.now(timezone.utc),
                                last_modified_by=user.user_id,
                                last_modified_date=datetime.now(timezone.utc)
                            )
                            db.session.add(spouse_dependent)
                            db.session.flush()
                            print(f"Created new dependent record for spouse: {spouse_name}")

                        # Create/update joint member entry if filing status is MFJ
                        current_filing_status = new_filing_status or original_filing_status
                        if current_filing_status == 'Married filing jointly':
                            existing_joint_member = CustomerJointMember.query.filter_by(
                                customer_id=customer.id,
                                tax_financialyear=tax_year,
                                member_name=spouse_name
                            ).first()

                            if not existing_joint_member:
                                joint_member = CustomerJointMember(
                                    tenant_id=user.tenant_id,
                                    tax_financialyear=tax_year,
                                    customer_id=customer.id,
                                    member_id=spouse_dependent.id,
                                    member_name=spouse_name
                                )
                                db.session.add(joint_member)
                                print(f"Created new joint member record for spouse (MFJ): {spouse_name}")
                            else:
                                existing_joint_member.last_modified_by = user.user_id
                                existing_joint_member.last_modified_date = datetime.now(timezone.utc)
                                print(f"Updated existing joint member record for spouse (MFJ): {spouse_name}")
                else:
                    print("No spouse information to process")

                # Process dependents
                dependents_data = extracted_data.get('dependents', [])
                existing_dependents = Dependent.query.filter_by(customer_id=customer.id).all()
                existing_dependent_names = {dep.name: dep for dep in existing_dependents}

                for dep_data in dependents_data:
                    dep_name = dep_data.get('name')
                    if dep_name in existing_dependent_names:
                        # Update existing dependent
                        dependent = existing_dependent_names[dep_name]
                        dependent.relationship = dep_data.get('relationship')
                        dependent.last_modified_by = user.user_id
                        dependent.last_modified_date = datetime.now(timezone.utc)
                    else:
                        # Create new dependent
                        new_dependent = Dependent(
                            customer_id=customer.id,
                            name=dep_name,
                            relationship=dep_data.get('relationship'),
                            created_by=user.user_id,
                            created_date=datetime.now(timezone.utc),
                            last_modified_by=user.user_id,
                            last_modified_date=datetime.now(timezone.utc)
                        )
                        db.session.add(new_dependent)

                # Get all document types from the database
                all_document_types = DocumentType.query.all()

                # Create dynamic mapping dictionary
                document_types_map = {}

                for doc_type in all_document_types:
                    # Create variations of the document type name
                    base_name = doc_type.type_name.replace('Form ', '').strip()

                    # Add various formats to the mapping
                    document_types_map[base_name] = doc_type.type_name  # e.g., "W-2" -> "Form W-2"
                    document_types_map[
                        f"Form {base_name}"] = doc_type.type_name  # e.g., "Form W-2" -> "Form W-2"
                    document_types_map[
                        base_name.replace('-', '')] = doc_type.type_name  # e.g., "W2" -> "Form W-2"

                    # Handle special cases for 1099 forms
                    if base_name.startswith('1099-'):
                        suffix = base_name.split('-')[1]
                        document_types_map[f"1099{suffix}"] = doc_type.type_name
                        document_types_map[f"1099 {suffix}"] = doc_type.type_name

                print(f"Created dynamic document type mapping: {document_types_map}")

                # Create a reverse lookup for document types
                document_types = {
                    dt.type_name: dt
                    for dt in all_document_types
                }

                # Process documents
                for doc in extracted_data['required_documents']:
                    incoming_doc_type = doc['document_type']
                    print(f"Processing document type: {incoming_doc_type}")

                    # Try to find the matching document type
                    normalized_doc_type = incoming_doc_type.replace('Form ', '').strip()
                    full_type_name = document_types_map.get(normalized_doc_type)
                    print("Original document:", normalized_doc_type)
                    print("Full type name:", full_type_name)
                    if not full_type_name:
                        print(f"Document type not found: {incoming_doc_type}")
                        available_types = list(set(document_types_map.values()))
                        return jsonify({
                            "error": f"Document type '{incoming_doc_type}' not found in database",
                            "available_types": available_types
                        }), 400

                    # Look up document type record
                    document_type_record = document_types.get(full_type_name)
                    if not document_type_record:
                        print(f"Document type record not found: {full_type_name}")
                        return jsonify({
                            "error": f"Document type record not found for '{full_type_name}'"
                        }), 400

                    tax_year = extracted_data.get('form_details', {}).get(
                        'filing_year',
                        datetime.now(timezone.utc).year
                    )
                    # print("\nVerifying foreign key references:")
                    # print(f"- Customer exists: {Customer.query.get(customer.id) is not None}")
                    # print(f"- Document Type exists: {DocumentType.query.get(document_type_record.document_type_id) is not None}")
                    # print(f"- Tax Financial Year exists: {TaxFinancialYear.query.filter_by(tax_year=tax_year).first() is not None}")
                    # print(f"- Customer Tax Financial exists: {CustomerTaxFinancial.query.filter_by(customer_id=customer.id, tax_financialyear=tax_year).first() is not None}")
                    # Create new document
                    try:
                        new_document = Document(
                            customer_id=customer.id,
                            document_type_id=document_type_record.document_type_id,
                            file_name=f"{incoming_doc_type}_{tax_year}.pdf",
                            file_path=f"/uploads/{incoming_doc_type}_{tax_year}.pdf",
                            file_size=0,
                            mime_type="application/pdf",
                            upload_date=None,
                            process_status='PENDING',
                            created_at=datetime.now(timezone.utc),
                            updated_at=datetime.now(timezone.utc),
                            status='active',
                            due_date=year_end_due_date,
                            tenant_id=user.tenant_id,
                            tax_year=int(tax_year),
                            created_by=user.user_id,
                            last_modified_by=user.user_id,
                            requirement_source=document_type_record.type_name,
                            is_deleted=False,
                            customer_taxfinancial=tax_financial_year.tax_year
                        )
                        db.session.add(new_document)
                        print(f"Added new document for type: {incoming_doc_type}")
                    except Exception as doc_error:
                        print(f"Error creating document: {str(doc_error)}")
                        raise

                db.session.commit()
                print("Transaction committed successfully")

            return jsonify({
                "message": "Declaration completed successfully",
                "user_id": user.user_id
            }), 200

        except SQLAlchemyError as db_error:
            db.session.rollback()
            print(f"Database error: {str(db_error)}")
            return jsonify({
                "error": "Database error occurred",
                "details": str(db_error)
            }), 500

    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        return jsonify({
            "error": "An unexpected error occurred",
            "details": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route('/api/questionnaire/document-mappings', methods=['GET'])
def get_document_mappings():
    try:
        # Get all document types from database
        document_types = DocumentType.query.all()

        # Initialize mappings dictionary with categories
        mappings = defaultdict(list)

        # Organize documents by category
        for doc in document_types:
            category = doc.category_name.lower() if doc.category_name else 'other'

            # Create document info dictionary
            doc_info = {
                'document_type_id': doc.document_type_id,
                'type_name': doc.type_name,
                'description': doc.description
            }

            # Normalize category name and map documents
            if category == 'retirement income and contributions':
                mappings['retirement distributions'].append(doc_info)
            elif category == 'self-employment and business income':
                mappings['business income'].append(doc_info)
            elif category == 'education expenses':
                mappings['education expenses'].append(doc_info)
            elif category == 'health care information':
                mappings['health savings account'].append(doc_info)
            elif category == 'investment income and expenses':
                mappings['investment income'].append(doc_info)
            elif category == 'rental income':
                mappings['rental income'].append(doc_info)
            elif category == 'income':
                if 'W-2' in doc.type_name:
                    mappings['w-2 salary/wages'].append(doc_info)
                elif '1099-NEC' in doc.type_name:
                    mappings['contractor payments (1099-nec)'].append(doc_info)
                else:
                    mappings['other income'].append(doc_info)
            elif category == 'foreign income and assets':
                mappings['foreign income or assets'].append(doc_info)
            else:
                mappings['other'].append(doc_info)

        return jsonify({
            'status': 'success',
            'mappings': dict(mappings)
        }), 200

    except Exception as e:
        print(f"Error fetching document mappings: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/establish_session', methods=['POST'])
def establish_session():
    try:
        data = request.json
        token = data.get('token')
        email = data.get('email')
        password = data.get('password')

        print(f"Establishing session for email: {email}")

        if not all([token, email, password]):
            return jsonify({'error': 'Missing required fields'}), 400

        # Verify user from declaration token
        user = User.query.filter_by(declaration_token=token).first()
        if not user:
            return jsonify({'error': 'Invalid declaration token'}), 401

        # Verify password
        if not check_password_hash(user.password_hash, password):
            return jsonify({'error': 'Invalid credentials'}), 401

        # Generate new auth token
        auth_token = jwt.encode({
            'user_id': user.user_id,
            'user_role': user.user_role,
            'email': user.email,
            'exp': datetime.now(timezone.utc) + timedelta(hours=24)
        }, app.config['SECRET_KEY'])

        # Update user status and session info
        user.is_declared = True
        user.status = 'active'
        user.last_login = datetime.now(timezone.utc)

        # Don't clear declaration token until successful commit
        db.session.commit()

        print(f"Session established successfully for user {user.user_id}")

        return jsonify({
            'auth_token': auth_token,
            'user_data': {
                'user_id': user.user_id,
                'email': user.email,
                'username': user.username,
                'user_role': user.user_role,
                'first_name': user.first_name,
                'last_name': user.last_name
            }
        }), 200

    except Exception as e:
        db.session.rollback()
        app.print(f"Session establishment error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/user/dashboard-data', methods=['GET'])
@token_required
def get_dashboard_data(current_user):
    try:
        # Get customer record
        customer = Customer.query.filter_by(user_id=current_user.user_id).first()
        if not customer:
            return jsonify({'error': 'Customer record not found'}), 404

        # Get documents with their types and categories
        documents = (db.session.query(Document, DocumentType)
                     .join(DocumentType)
                     .filter(Document.customer_id == customer.id)
                     .all())

        # Format document data
        formatted_docs = []
        for doc, doc_type in documents:
            formatted_docs.append({
                'document_id': doc.document_id,
                'type_name': doc_type.type_name,
                'category': doc_type.category_name,
                'status': doc.status,
                'due_date': doc.due_date.isoformat() if doc.due_date else None,
                'upload_date': doc.upload_date.isoformat() if doc.upload_date else None,
                'file_name': doc.file_name
            })

        return jsonify({
            'user': {
                'id': current_user.user_id,
                'email': current_user.email,
                'name': f"{current_user.first_name} {current_user.last_name}",
                'role': current_user.user_role
            },
            'documents': formatted_docs
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/declaration/form-data/<token>', methods=['GET'])
@declaration_token_required
def get_form_data(user):
    """Get all form data for a user's declaration"""
    try:
        # Get the latest document submitted by the user
        document = Document.query.filter_by(
            customer_id=user.user_id
        ).order_by(Document.created_at.desc()).first()

        if not document:
            return jsonify({
                "message": "No document found",
                "data": None
            }), 404

        # Get extracted data
        extracted_data = ExtractedData.query.filter_by(
            document_id=document.document_id
        ).all()

        # Format the data for frontend
        formatted_data = format_extracted_data(extracted_data)

        return jsonify({
            "message": "Data retrieved successfully",
            "data": formatted_data
        }), 200

    except Exception as e:
        print(f"Error retrieving form data: {str(e)}")
        return jsonify({
            "message": "Error retrieving form data",
            "error": str(e)
        }), 500


@app.route('/api/declaration/update-section/<token>', methods=['POST'])
@declaration_token_required
def update_form_section(user):
    """Update a specific section of the form"""
    try:
        data = request.json
        section = data.get('section')
        section_data = data.get('data')

        if not section or not section_data:
            return jsonify({
                "message": "Missing required fields",
            }), 400

        # Update or create the section data
        declaration_data = DeclarationFormData.query.filter_by(
            user_id=user.user_id,
            section=section
        ).first()

        if declaration_data:
            declaration_data.data = section_data
            declaration_data.last_modified_date = datetime.now(timezone.utc)
        else:
            declaration_data = DeclarationFormData(
                user_id=user.user_id,
                section=section,
                data=section_data,
                created_date=datetime.now(timezone.utc),
                last_modified_date=datetime.now(timezone.utc)
            )
            db.session.add(declaration_data)

        db.session.commit()

        return jsonify({
            "message": "Section updated successfully",
            "section": section
        }), 200

    except Exception as e:
        db.session.rollback()
        print(f"Error updating form section: {str(e)}")
        return jsonify({
            "message": "Error updating form section",
            "error": str(e)
        }), 500


def format_extracted_data(extracted_data):
    """Format extracted data for frontend consumption"""
    formatted = {
        'personal_information': {},
        'dependents': [],
        'income': {
            'w2_forms': [],
            'other_income': []
        },
        'deductions': {},
        'health_care': {},
        'education': {},
        'retirement': {},
        'business': [],
        'purchases': []
    }

    # Group extracted data by section
    for data in extracted_data:
        section, field = data.field_name.split(' - ', 1)
        value = data.field_value

        if section == 'Personal Information':
            formatted['personal_information'][field] = value
        elif section == 'Dependent':
            # Parse dependent number and field
            dep_num, dep_field = field.split(' - ')
            dep_index = int(dep_num) - 1

            # Ensure list has enough elements
            while len(formatted['dependents']) <= dep_index:
                formatted['dependents'].append({})

            formatted['dependents'][dep_index][dep_field] = value
        # Add other section handling as needed

    return formatted


def process_document(document_id, user_id, doc_type):
    print(f"Started processing document {document_id}")
    start_time = time.time()

    document = Document.query.get(document_id)
    if not document:
        print(f"Document not found: {document_id}")
        return {
            'status': 'error',
            'message': 'Document not found',
            'validation_result': False
        }

    current_user = User.query.get(user_id)
    if not current_user:
        print(f"User not found: {user_id}")
        return {
            'status': 'error',
            'message': 'User not found',
            'validation_result': False
        }

    try:
        # Update status to PROCESSING
        document.status = 'PROCESSING'
        db.session.commit()

        # Perform OCR
        print("Performing OCR")
        print("File path:", document.file_path)
        ocr_result = perform_ocr(document.file_path)
        # print("OCR Result:", ocr_result['ocr_result'])

        print("OCR Result Status:", ocr_result['ocr_status'])
        print("OCR RESULT:", ocr_result)
        if not ocr_result['ocr_status']:
            document.status = 'ERROR'
            db.session.commit()

            # Check for password protected error
            if ocr_result.get('error_type') == 'password_protected':
                print("Returning the password-protected error message")
                return {
                    'status': 'error',
                    'message': ocr_result['error_message'],
                    'error_type': 'password_protected',
                    'validation_result': False,
                    'validation_message': 'Password protected PDF files are not supported'
                }
            print("Returning the general error message")
            # Handle other OCR errors
            return {
                'status': 'error',
                'message': ocr_result.get('error_message', 'OCR processing failed'),
                'error_type': ocr_result.get('error_type', 'general_error'),
                'validation_result': False,
                'validation_message': 'Unable to extract text from the file'
            }

        # Save OCR text
        try:
            ocr_filename = f"{document.document_id}_ocr.txt"
            ocr_path = os.path.join(app.config['OCR_FOLDER'], ocr_filename)

            # Ensure OCR directory exists
            os.makedirs(app.config['OCR_FOLDER'], exist_ok=True)

            with open(ocr_path, 'w', encoding='utf-8') as f:
                f.write(ocr_result['ocr_result'])

            # Save OCR result to database
            ocr_entry = OCRResult(
                document_id=document.document_id,
                raw_text=ocr_result['ocr_result'],
                confidence_score=ocr_result.get('confidence_score', 0),
                processing_time=int(ocr_result.get('processing_time', 0)),
                ocr_engine="Azure Form Recognizer",
                status='SUCCESS'
            )
            db.session.add(ocr_entry)
            db.session.commit()

            print("Extracting data")
            extracted_data = extract_data(ocr_result['ocr_result'], prompt)
            print("CLAUDE raw response:", extracted_data['raw_response'])

            # Save extracted data
            raw_claude_filename = f"{document.document_id}_claude_raw.txt"
            raw_claude_path = os.path.join(app.config['EXTRACTED_CLAUDE'], raw_claude_filename)

            # Ensure EXTRACTED_CLAUDE directory exists
            os.makedirs(app.config['EXTRACTED_CLAUDE'], exist_ok=True)

            with open(raw_claude_path, 'w', encoding='utf-8') as f:
                f.write(extracted_data['raw_response'])

            print("-----------------------------------------------------")

            # Parse extracted data
            parsed_data = parse_extracted_data(extracted_data['raw_response'])
            print("CLAUDE parsed data:", parsed_data)

            if not parsed_data:
                document.status = 'ERROR'
                db.session.commit()
                return {
                    'status': 'error',
                    'message': 'Data extraction failed',
                    'validation_result': False,
                    'validation_message': 'Unable to parse document data'
                }

            # Save parsed data
            parsed_filename = f"{document.document_id}_parsed.txt"
            parsed_path = os.path.join(app.config['EXTRACTED_PARSED'], parsed_filename)

            # Ensure EXTRACTED_PARSED directory exists
            os.makedirs(app.config['EXTRACTED_PARSED'], exist_ok=True)

            with open(parsed_path, 'w', encoding='utf-8') as f:
                json.dump(parsed_data, f, ensure_ascii=False, indent=4)

            # Save extracted data to database
            for section, fields in parsed_data.items():
                for field_name, field_data in fields.items():
                    full_field_name = f"{section} - {field_name}"
                    truncated_field_name = full_field_name[:255]
                    truncated_field_value = str(field_data.get('value', ''))[:65535]

                    extracted_data_entry = ExtractedData(
                        document_id=document.document_id,
                        field_name=truncated_field_name,
                        field_value=truncated_field_value,
                        confidence_score=field_data.get('confidence', 0),
                        status='SUCCESS' if extracted_data['claude_status'] else 'FAILURE'
                    )
                    db.session.add(extracted_data_entry)
            db.session.commit()

            # Validate data
            print("Validating data")
            is_valid, validation_message = validate_data(parsed_data, current_user.username, doc_type)

            # Save validation result
            validation_entry = ValidationResult(
                document_id=document.document_id,
                is_valid=is_valid,
                validation_message=validation_message
            )
            db.session.add(validation_entry)
            db.session.commit()

            if is_valid:
                document.status = 'PROCESSED'
                print(f"Document {document_id} validated successfully: {validation_message}")
            else:
                document.status = 'VALIDATION_FAILED'
                app.logger.warning(f"Document {document_id} validation failed: {validation_message}")

            end_time = time.time()
            total_time = end_time - start_time

            print(f"Document {document_id} processed in {total_time:.2f} seconds")
            return {
                'status': 'success',
                'document_id': document_id,
                'processing_time': total_time,
                'validation_result': is_valid,
                'validation_message': validation_message
            }

        except Exception as e:
            print(f"Error processing document data: {str(e)}")
            document.status = 'ERROR'
            db.session.commit()
            return {
                'status': 'error',
                'message': f'Data processing failed: {str(e)}',
                'validation_result': False,
                'validation_message': str(e)
            }

    except Exception as e:
        print(f"Error processing document {document_id}: {str(e)}")
        document.status = 'ERROR'
        db.session.commit()
        return {
            'status': 'error',
            'message': f'Processing failed: {str(e)}',
            'validation_result': False,
            'validation_message': str(e)
        }


@app.route('/api/documents/<int:document_id>/preview', methods=['POST'])
@token_required
def serve_document_preview(current_user, document_id):
    try:
        print("Inside the documents preview")
        user = request.get_json('User')
        print("USER:", user)
        current_user = user['User']
        print("CURRENT USER:", current_user)
        document = Document.query.get(document_id)
        print("Document:", document)
        if not document:
            print("Document not found")
            return jsonify({"error": "Document not found"}), 404

        # Check user roles and permissions
        if current_user['user_role'] == 'customer':
            # Check if document belongs to the customer
            customer = Customer.query.filter_by(user_id=current_user['user_id']).first()
            if not customer or document.customer_id != customer.id:
                print("Unauthorized access")
                return jsonify({"error": "Unauthorized access"}), 403
        elif current_user['user_role'] not in ['support_agent', 'admin', 'super_admin']:
            print("Unauthorized user role")
            return jsonify({"error": "Unauthorized access"}), 403

        # Check if we have the file in the database
        if document.original_file:
            print("Serving file from database")
            # Create BytesIO object from stored binary data
            file_data = io.BytesIO(document.original_file)

            # Send file from memory
            return send_file(
                file_data,
                mimetype=document.mime_type,
                as_attachment=False,
                download_name=document.file_name
            )

        # Fallback to file system if database storage is empty
        if os.path.exists(document.file_path):
            print("Serving file from file system")
            return send_file(
                document.file_path,
                mimetype=document.mime_type,
                as_attachment=False,
                download_name=document.file_name
            )

        return jsonify({"error": "Document content not found"}), 404

    except Exception as e:
        print(f"Error serving document: {str(e)}")
        return jsonify({"error": f"Failed to serve document: {str(e)}"}), 500


@app.route('/verify_upload/<int:document_id>', methods=['GET'])
@token_required
def verify_upload(current_user, document_id):
    try:
        # Get document
        document = Document.query.get(document_id)
        if not document:
            return jsonify({"error": "Document not found"}), 404

        # Check authorization
        if document.customer_id != current_user.user_id and not current_user.is_admin:
            return jsonify({"error": "Unauthorized access"}), 403

        # Verify storage
        db_file_exists = document.original_file is not None
        db_file_size = len(document.original_file) if document.original_file else 0
        disk_file_exists = os.path.exists(document.file_path)
        disk_file_size = os.path.getsize(document.file_path) if disk_file_exists else 0

        # Generate verification info
        verification_info = {
            "document_id": document_id,
            "file_name": document.file_name,
            "database_storage": {
                "exists": db_file_exists,
                "size": db_file_size,
                "preview": document.original_file[:20].hex() if db_file_exists else None
            },
            "disk_storage": {
                "exists": disk_file_exists,
                "size": disk_file_size,
                "path": document.file_path
            },
            "metadata": {
                "mime_type": document.mime_type,
                "recorded_size": document.file_size,
                "upload_date": document.upload_date.isoformat() if document.upload_date else None,
                "status": document.status
            },
            "verification_result": {
                "sizes_match": db_file_size == disk_file_size == document.file_size,
                "storage_complete": db_file_exists and disk_file_exists,
                "overall_status": "valid" if (db_file_exists and disk_file_exists and
                                              db_file_size == disk_file_size == document.file_size) else "invalid"
            }
        }

        return jsonify(verification_info)

    except Exception as e:
        return jsonify({
            "error": "Verification failed",
            "message": str(e)
        }), 500


@app.route('/upload', methods=['POST'])
@token_required
def upload_file(current_user):
    file_path = None
    try:
        print("Starting upload process")

        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400

        file = request.files['file']
        doc_type_name = request.form.get('doc_type')
        document_id = request.form.get('document_id')

        print(f"Received upload request - doc_type: {doc_type_name}")
        print(f"Doc name: {doc_type_name} \n Doc id: {document_id}")

        # Get customer record
        customer = Customer.query.filter_by(user_id=current_user.user_id).first()
        if not customer:
            return jsonify({"error": "Customer record not found"}), 404

        document = Document.query.get(document_id)

        def normalize_document_type(doc_type):
            """Normalize document type by handling various formats."""
            doc_type = str(doc_type).lower().strip()

            # Remove 'form' prefix if present
            doc_type = doc_type.replace('form ', '').strip()

            # Handle Schedule format variations
            if 'schedule' in doc_type:
                # Extract schedule number or letter
                schedule_match = re.search(r'schedule\s+([0-9a-z]+)', doc_type)
                if schedule_match:
                    return f"schedule {schedule_match.group(1)}"

            # Remove form references in parentheses
            doc_type = re.sub(r'\s*\([^)]*\)', '', doc_type)
            return doc_type.strip()

        # Get all document types and find a match
        normalized_input = normalize_document_type(doc_type_name)
        print(f"Normalized input: {normalized_input}")

        matching_doc_type = None
        all_doc_types = DocumentType.query.all()

        # Print all available document types for debugging
        print("Available document types:")
        for dt in all_doc_types:
            normalized_dt = normalize_document_type(dt.type_name)
            print(f"Original: {dt.type_name} -> Normalized: {normalized_dt}")
            if normalized_dt == normalized_input:
                matching_doc_type = dt
                print(f"Found match: {dt.type_name}")
                break

        if not matching_doc_type:
            available_types = [dt.type_name for dt in all_doc_types]
            return jsonify({
                "error": f"Document type {doc_type_name} not found",
                "available_types": available_types
            }), 404

        # Find existing document record using the document from query
        if document:
            existing_document = document
        else:
            existing_document = Document.query.filter_by(
                customer_id=customer.id,
                document_type_id=matching_doc_type.document_type_id,
                tax_year=datetime.now().year - 1
            ).filter(
                Document.status != 'SUBMITTED'
            ).first()

        if not existing_document:
            return jsonify({"error": "No pending document requirement found"}), 404

        try:
            # Process file upload
            file_content = file.read()
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = secure_filename(f"{file.filename}")
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

            # Ensure upload directory exists
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

            # Save file
            with open(file_path, 'wb') as f:
                f.write(file_content)

            # Only update file-related fields after successful file save
            existing_document.file_name = filename
            existing_document.file_path = file_path
            existing_document.file_size = len(file_content)
            existing_document.mime_type = file.content_type or mimetypes.guess_type(file_path)[0]
            existing_document.last_modified_by = current_user.user_id
            existing_document.original_file = file_content
            existing_document.status = 'PROCESSING'
            # Don't update upload_date yet
            db.session.commit()

        except Exception as e:
            print(f"File save error: {str(e)}")
            return jsonify({
                "error": "File upload failed",
                "message": str(e),
                "status": "ERROR"
            }), 500

        print(f"File saved: {existing_document.document_id}")
        doc_type = DocumentType.query.get(document.document_type_id)

        # Process and validate document
        process_result = process_document(existing_document.document_id, current_user.user_id, doc_type.type_name)

        if process_result['status'] == 'error':
            existing_document.status = 'ERROR'
            existing_document.process_status = 'ERROR'
            db.session.commit()

            # Remove the uploaded file if processing failed
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    print(f"Error removing file after processing failure: {str(e)}")

            return jsonify({
                "error": process_result.get('message', 'Processing failed'),
                "status": "ERROR",
                "document_id": existing_document.document_id,
                "validation_result": False,
                "validation_message": process_result.get('error')
            }), 400

        # Update document status based on process result
        if process_result['status'] == 'success':
            if process_result.get('validation_result'):
                existing_document.status = 'SUBMITTED'
                # Only update upload_date after successful processing
                existing_document.upload_date = datetime.now(timezone.utc)
            else:
                existing_document.status = 'VALIDATION_FAILED'
        else:
            existing_document.status = 'ERROR'

        existing_document.process_status = 'COMPLETED' if process_result['status'] == 'success' else 'ERROR'
        db.session.commit()

        print(f"Document status updated to: {existing_document.status}")

        response_data = {
            "message": "File processed successfully" if process_result[
                'validation_result'] else "File uploaded but validation failed",
            "document_id": existing_document.document_id,
            "status": existing_document.status.strip(),
            "validation_result": process_result.get('validation_result'),
            "validation_message": process_result.get('validation_message')
        }

        print(f"Upload process completed: {response_data}")
        return jsonify(response_data), 200

    except Exception as e:
        print(f"Upload error: {str(e)}")
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

        if 'existing_document' in locals():
            existing_document.status = 'ERROR'
            existing_document.process_status = 'ERROR'
            db.session.commit()

        return jsonify({
            "error": str(e),
            "status": "ERROR",
            "message": "An unexpected error occurred during upload"
        }), 500


@app.route('/add_file', methods=['POST'])
@token_required
def add_file(current_user):
    try:
        data = request.get_json()
        document_type_id = data.get('document_type_id')
        category = data.get('category')

        if not document_type_id:
            return jsonify({"error": "Document type ID is required"}), 400

        # Get customer record
        customer = Customer.query.filter_by(user_id=current_user.user_id).first()
        if not customer:
            return jsonify({"error": "Customer record not found"}), 404

        # Get document type details
        doc_type = DocumentType.query.get(document_type_id)
        if not doc_type:
            return jsonify({"error": "Invalid document type"}), 400
        print("Cust:", customer.id, "\n\n", customer)
        tax_year = CustomerTaxFinancial.query.filter_by(customer_id=customer.id).first()
        print("tax_year", tax_year)
        # Create new document record
        new_document = Document(
            customer_id=customer.id,
            document_type_id=document_type_id,
            file_name=f"{doc_type.type_name}_additional",
            file_path=f"/uploads/{doc_type.type_name}_additional",
            file_size=0,
            mime_type="application/pdf",
            upload_date=datetime.now(timezone.utc),
            process_status='PENDING',
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status='active',
            tenant_id=current_user.tenant_id,
            tax_year=datetime.now().year - 1,  # Previous tax year
            created_by=current_user.user_id,
            last_modified_by=current_user.user_id,
            requirement_source=doc_type.type_name,
            due_date=datetime.now(timezone.utc) + timedelta(days=30),  # Set due date to 30 days from now
            is_deleted=False,
            customer_taxfinancial=tax_year.tax_financialyear
        )

        db.session.add(new_document)

        try:
            db.session.commit()

            # Return the newly created document details
            return jsonify({
                "message": "New document slot added successfully",
                "document": {
                    "document_id": new_document.document_id,
                    "type_name": doc_type.type_name,
                    "category": category,
                    "status": "active",
                    "due_date": new_document.due_date.isoformat() if new_document.due_date else None
                }
            }), 201

        except Exception as e:
            db.session.rollback()
            print(f"Database error while adding document: {str(e)}")
            return jsonify({"error": f"Failed to add document: {str(e)}"}), 500

    except Exception as e:
        print(f"Error adding document: {str(e)}")
        return jsonify({"error": f"Error adding document: {str(e)}"}), 500


@app.route('/check_document_limit/<int:document_type_id>', methods=['GET'])
@token_required
def check_document_limit(current_user, document_type_id):
    try:
        # Get customer record
        customer = Customer.query.filter_by(user_id=current_user.user_id).first()
        if not customer:
            return jsonify({"error": "Customer record not found"}), 404

        # Get existing documents count for this type
        existing_docs_count = Document.query.filter_by(
            customer_id=customer.id,
            document_type_id=document_type_id,
            tax_year=datetime.now().year - 1
        ).count()

        # You can set a maximum limit here (e.g., 5 documents per type)
        max_limit = 5
        can_add_more = existing_docs_count < max_limit

        return jsonify({
            "can_add_more": can_add_more,
            "current_count": existing_docs_count,
            "max_limit": max_limit
        }), 200

    except Exception as e:
        print(f"Error checking document limit: {str(e)}")
        return jsonify({"error": f"Error checking document limit: {str(e)}"}), 500


@app.route('/status/<int:document_id>', methods=['GET'])
@token_required
def get_status(current_user, document_id):
    document = Document.query.get(document_id)
    if not document:
        return jsonify({"error": "Document not found"}), 404
    if document.customer_id != current_user.user_id and current_user.user_role not in ['administrator', 'super_admin']:
        return jsonify({"error": "Unauthorized access"}), 403
    return jsonify({
        "status": document.status,
        "file_name": document.file_name,
        "created_at": document.created_at.isoformat(),
        "ocr_result": OCRResult.query.filter_by(
            document_id=document.document_id).first().raw_text if OCRResult.query.filter_by(
            document_id=document.document_id).first() else None,
        "extracted_data": {data.field_name: data.field_value for data in
                           ExtractedData.query.filter_by(document_id=document.document_id).all()}
    }), 200


@app.route('/get_original_file/<int:document_id>', methods=['GET'])
@token_required
def get_original_file(current_user, document_id):
    try:
        document = Document.query.get(document_id)
        if not document:
            return jsonify({"error": "Document not found"}), 404

        if document.customer_id != current_user.user_id and not current_user.is_admin:
            return jsonify({"error": "Unauthorized access"}), 403

        if not document.original_file:
            return jsonify({"error": "No original file stored"}), 404

        return send_file(
            io.BytesIO(document.original_file),
            mimetype=document.mime_type,
            as_attachment=True,
            download_name=document.file_name
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/user_documents', methods=['GET'])
@token_required
def get_user_documents(current_user):
    try:
        print(f"\n=== Fetching documents for user {current_user.user_id} ===")

        # Use ORM to query documents
        documents = Document.query.filter(
            Document.customer_id == current_user.user_id,
            Document.requirement_source.isnot(None)
        ).order_by(Document.created_at.desc()).all()

        document_list = []
        for doc in documents:
            document_info = {
                'document_id': doc.document_id,
                'required_fields': [doc.requirement_source] if doc.requirement_source else [],
                'file_name': doc.file_name,
                'status': doc.status.strip() if doc.status else 'PENDING',
                'due_date': doc.due_date.isoformat() if doc.due_date else None,
                'priority': doc.priority,
                'tax_year': doc.tax_year
            }
            print(f"Processing document: {document_info}")
            document_list.append(document_info)

        print(f"Found {len(document_list)} documents")
        return jsonify({'documents': document_list})

    except Exception as e:
        print(f"Error fetching user documents: {str(e)}")
        return jsonify({"error": "Failed to fetch documents"}), 500


def perform_ocr(file_path):
    # endpoint = 'https://taxation.cognitiveservices.azure.com/'
    # key = '8110ac520cbf480fa99e48ef2540f0dd'
    endpoint = config['API']['AZURE']['ENDPOINT']
    key = config['API']['AZURE']['KEY']

    document_analysis_client = DocumentAnalysisClient(
        endpoint=endpoint, credential=AzureKeyCredential(key)
    )
    print("Performing OCR In perform_ocr()")
    start_time = time.time()
    try:
        with open(file_path, "rb") as pdf_file:
            poller = document_analysis_client.begin_analyze_document("prebuilt-document", pdf_file, locale="en")
            result = poller.result()

        extracted_text = ""

        for page in result.pages:
            for line in page.lines:
                extracted_text += line.content + "\n"

        end_time = time.time()
        processing_time = end_time - start_time
        print("OCR DATA:", extracted_text.strip())
        return {
            'ocr_result': extracted_text.strip(),
            'ocr_status': True,
            'processing_time': round(processing_time * 1000)  # Convert to milliseconds
        }


    except Exception as e:
        print(f"OCR Error: {str(e)}")
        error_message = str(e)
        # Check for password protected error
        if "UnsupportedContent" in error_message and "password protected" in error_message.lower():
            print("Returning the password-protected error message")
            return {
                'ocr_result': '',
                'ocr_status': False,
                'error_type': 'password_protected',
                'error_message': 'The PDF file is password protected. Please provide an unprotected file.',
                'processing_time': 0
            }
        # Handle other errors
        print("Returning the general error message")
        return {
            'ocr_result': '',
            'ocr_status': False,
            'error_type': 'general_error',
            'error_message': f'OCR processing failed: {error_message}',
            'processing_time': 0
        }


def extract_data(ocr_text, prompt):
    client = anthropic.Anthropic(
        api_key=config['API']['CLAUDE_API']
    )
    claude_status = False
    print("Extracting data with Claude")
    try:
        message = client.messages.create(
            # model="claude-3-haiku-20240307",
            model="claude-3-5-sonnet-20240620",
            max_tokens=4000,
            temperature=0,
            system=prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": ocr_text,
                        }
                    ]
                }
            ]
        )

        extracted_data = message.content[0].text
        # Parse the extracted_data string into a dictionary
        # parsed_data = parse_extracted_data(extracted_data)
        return {
            'raw_response': extracted_data,
            # 'parsed': parsed_data,
            'claude_status': True
        }

    except Exception as e:
        print(f"Error calling Claude API: {str(e)}")
        return {
            'raw_response': '',
            # 'parsed': {},
            'claude_status': False
        }


def parse_extracted_data(response):
    """
    Parse Claude's text response into a structured dictionary using key-based mapping.

    Args:
        response (str): Raw text response from Claude containing document information

    Returns:
        dict: Structured dictionary containing parsed document information
    """
    # Define standard sections
    extracted_data = {
        'Document Identification': {},
        'Payer/Employer Information': {},
        'Recipient/Employee Information': {},
        'Financial Information': {},
        'Additional Information': {}
    }

    # Define key mappings for different sections
    section_key_mappings = {
        'Payer/Employer Information': [
            'employer', 'payer', 'company', 'business', 'ein',
            "employer's", "payer's", 'corporation name'
        ],
        'Recipient/Employee Information': [
            'employee', 'recipient', 'worker', 'contractor', 'ssn',
            "employee's", "recipient's"
        ],
        'Financial Information': [
            'box', 'wages', 'tax', 'compensation', 'income', 'payment',
            'earnings', 'tips', 'medicare', 'social security', 'withholding'
        ],
        'Additional Information': [
            'control', 'department', 'corporation', 'reference', 'misc',
            'additional', 'other', 'notes'
        ]
    }

    # Standard fields that should appear in specific sections
    standard_fields = {
        'Name': 'Document Identification',
        'Address': 'Document Identification',
        'Identification Number': 'Document Identification',
        'Document Type': 'Document Identification',
        'Tax Year': 'Document Identification'
    }

    current_section = None
    lines = response.split('\n')

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Check for section headers
        if line.endswith(':'):
            section_name = line[:-1].strip()
            if section_name in extracted_data:
                current_section = section_name
            continue

        # Handle list items and key-value pairs
        if current_section and '-' in line:
            # Remove bullet points or dashes at the start
            line = line.lstrip('- *')

            # Split on first occurrence of ':'
            parts = line.split(':', 1)

            if len(parts) == 2:
                key, value = parts
                key = key.strip()
                value = value.strip()

                # Determine appropriate section based on key words
                target_section = current_section
                for section, keywords in section_key_mappings.items():
                    if any(keyword in key.lower() for keyword in keywords):
                        target_section = section
                        break

                # Handle standard fields
                if key in standard_fields:
                    # Check if this is the second occurrence of a standard field
                    if (key == 'Name' and 'Name' in extracted_data['Document Identification'] and
                            'employer' in value.lower()):
                        target_section = 'Payer/Employer Information'
                    elif (key == 'Name' and 'Name' in extracted_data['Document Identification'] and
                          'employee' in value.lower()):
                        target_section = 'Recipient/Employee Information'
                    elif (key == 'Address' and 'Address' in extracted_data['Document Identification']):
                        # Determine if this is employer or employee address based on context
                        if any(keyword in value.lower() for keyword in
                               section_key_mappings['Payer/Employer Information']):
                            target_section = 'Payer/Employer Information'
                        else:
                            target_section = 'Recipient/Employee Information'
                    elif (key == 'Identification Number' and
                          'Identification Number' in extracted_data['Document Identification']):
                        # Check format to determine if it's an EIN or SSN
                        if 'xxx' in value.lower() or 'ssn' in value.lower():
                            target_section = 'Recipient/Employee Information'
                        else:
                            target_section = 'Payer/Employer Information'

                # Special handling for Box entries
                if key.lower().startswith('box '):
                    target_section = 'Financial Information'

                # Add the key-value pair to the appropriate section
                extracted_data[target_section][key] = {
                    'value': value,
                    'confidence': 1.0
                }

    # Clean up any empty sections
    extracted_data = {k: v for k, v in extracted_data.items() if v}

    return extracted_data


def parseForm1040(claude_response_text):
    """
    Parse the raw response text from Claude and extract the JSON structure.

    :param claude_response_text: Raw string containing Claude's response with key-value pairs (including non-JSON parts).
    :return: Parsed dictionary if successful, otherwise raise an error.
    """
    try:
        # Use regex to extract the JSON content (everything between the first and last curly braces)
        json_text = re.search(r'\{.*\}', claude_response_text, re.DOTALL)

        if not json_text:
            raise ValueError("Document Type Not Matched")

        # Load the extracted JSON content
        parsed_data = json.loads(json_text.group())
        return parsed_data

    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse Claude's response: {str(e)}")


def get_value_ignore_case(data_dict, key):
    if not isinstance(data_dict, dict):
        return None

    # Try exact match first
    if key in data_dict:
        return data_dict[key]

    # Try case-insensitive match
    key_lower = key.lower()
    for k, v in data_dict.items():
        if isinstance(k, str) and k.lower() == key_lower:
            return v

    return None


def validate_form1040_response(form_data, user_data):
    try:
        # Log incoming data for debugging
        print("Starting Form 1040 validation")
        print(f"User data: {user_data}")

        validation_errors = []

        # 1. Validate Form Type
        form_type = form_data.get('form_details', {}).get('form_type')
        if not form_type or form_type != '1040':
            validation_errors.append("Invalid form type. Expected Form 1040")

        # 2. Validate Filing Year
        filing_year = form_data.get('form_details', {}).get('filing_year')
        try:
            if int(filing_year) != datetime.now().year - 2:
                validation_errors.append(f"Invalid filing year. Expected {datetime.now().year-2}, got {filing_year}")
        except (ValueError, TypeError):
            validation_errors.append("Invalid filing year format")

        # 3. Validate Taxpayer Name
        taxpayer_first_name = form_data.get('personal_information', {}).get('taxpayer', {}).get('first_name',
                                                                                                '').lower()
        taxpayer_last_name = form_data.get('personal_information', {}).get('taxpayer', {}).get('last_name', '').lower()
        user_full_name = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".lower()
        taxpayer_name = taxpayer_first_name + " " + taxpayer_last_name
        # Log name comparison for debugging
        print(f"Comparing names - Form: {taxpayer_name}, User: {user_full_name}")

        if not taxpayer_name or not user_full_name:
            validation_errors.append("Missing taxpayer name or user data")
        elif taxpayer_name.replace(' ', '') != user_full_name.replace(' ', ''):
            validation_errors.append(f"Taxpayer name mismatch. Expected {user_full_name}, got {taxpayer_name}")

        # 4. Validate Required Documents
        required_docs = form_data.get('required_documents', [])
        mandatory_docs = [doc for doc in required_docs if doc.get('mandatory', False)]
        if not mandatory_docs:
            validation_errors.append("No mandatory supporting documents identified")

        # 5. Basic SSN Format Validation
        # taxpayer_ssn = form_data.get('personal_information', {}).get('taxpayer', {}).get('ssn')
        # if not validate_ssn_format(taxpayer_ssn):
        #     validation_errors.append("Invalid taxpayer SSN format")

        # Create validation result
        if validation_errors:
            error_message = "; ".join(validation_errors)
            app.logger.warning(f"Form 1040 validation failed: {error_message}")
            return False, error_message

        success_message = "Form 1040 validation successful. All required fields verified."
        print(success_message)
        return True, success_message

    except Exception as e:
        error_message = f"Validation error: {str(e)}"
        print(error_message)
        return False, error_message


def validate_document_names(user, recipient_name, extracted_data):
    try:
        print("Starting name validation...")

        # Get customer details including filing status
        customer = Customer.query.filter_by(user_id=user.user_id).first()
        if not customer:
            print("Validation Failed: Customer information not found")
            return False, "Customer information not found"

        # Get tax year from extracted data
        tax_year = extracted_data.get('Financial Information', {}).get('Tax Year', {}).get('value')
        print("filing Year:", tax_year)
        # Get current filing status
        customer_tax_financial = CustomerTaxFinancial.query.filter_by(
            customer_id=customer.id,
            tax_financialyear=tax_year,
            status='active'
        ).first()
        print("Customer tax financial:", customer_tax_financial.tax_financialyear)
        if not customer_tax_financial:
            print("Validation Failed: Tax financial information not found")
            return False, "Tax financial information not found"

        filing_status = customer_tax_financial.filing_type
        print(f"Filing Status: {filing_status}")

        def clean_name(name):
            """Remove special characters and standardize spacing"""
            import re
            if not name:
                return ""
            # Convert to uppercase and remove special characters
            cleaned = re.sub(r'[^\w\s]', '', name.upper())
            # Standardize spacing
            cleaned = ' '.join(cleaned.split())
            return cleaned

        # Clean recipient name
        if isinstance(recipient_name, dict) and 'value' in recipient_name:
            recipient_name = recipient_name['value']
        recipient_name_clean = clean_name(recipient_name)

        # Get primary taxpayer name
        primary_full_name = f"{user.first_name} {user.last_name}"
        primary_name_clean = clean_name(primary_full_name)
        print(f"Primary Taxpayer Name: {primary_name_clean}")

        # Check if names match with primary taxpayer
        if recipient_name_clean == primary_name_clean:
            print("Name matched with primary taxpayer")
            return True, "Name validation successful"

        # If filing jointly, check spouse name
        if filing_status == "Married filing jointly":
            # Get spouse details from dependent table
            spouse = Dependent.query.filter_by(
                customer_id=customer.id,
                relationship='Spouse'
            ).first()

            if spouse:
                spouse_name_clean = clean_name(spouse.name)
                print(f"Spouse Name: {spouse_name_clean}")

                # Check if names match with spouse
                if recipient_name_clean == spouse_name_clean:
                    print("Name matched with spouse")
                    return True, "Name validation successful"

                print(f"Name mismatch - Document: {recipient_name_clean}, "
                      f"Primary: {primary_name_clean}, Spouse: {spouse_name_clean}")
                return False, "Name does not match either primary taxpayer or spouse"
            else:
                print("Spouse information not found in database")
                return False, "Spouse information not found for joint filing"

        # If not filing jointly or name didn't match either party
        print(f"Name mismatch - Document: {recipient_name_clean}, Database: {primary_name_clean}")
        return False, "Name does not match registered taxpayer"

    except Exception as e:
        print(f"Validation Failed: Error during name validation: {str(e)}")
        return False, f"Error during name validation: {str(e)}"


def validate_data(extracted_data, login_username, doc_type1):
    def normalize_document_type(doc_type):
        """Normalize document type by handling various formats and extracting base type."""
        doc_type = str(doc_type).lower().strip()

        # Remove 'form' prefix if present
        doc_type = doc_type.replace('form ', '').strip()

        # Handle Schedule format variations
        if 'schedule' in doc_type:
            # Extract just the schedule letter using regex
            schedule_match = re.search(r'schedule\s+([a-z])', doc_type)
            if schedule_match:
                return f"schedule {schedule_match.group(1)}"

        # Remove form references in parentheses
        doc_type = re.sub(r'\s*\([^)]*\)', '', doc_type)

        return doc_type.strip()

    print("\nParsed data in validation data():", extracted_data, "\nThe current user is:", login_username)
    print("Type of extracted_data:", type(extracted_data))

    if not extracted_data or not isinstance(extracted_data, dict):
        print("validation Failed: It is not a Dictionary")
        return False, "No valid data extracted"

    # Validate document identification
    doc_identification = extracted_data.get('Document Identification') or {}
    if not doc_identification:
        print("validation Failed: Document Identification")
        return False, "Document identification information missing"

    doc_type = get_value_ignore_case(doc_identification, 'Document Type')
    if not doc_type:
        print("validation Failed: Document Type")
        return False, "Document type not specified in extracted data"

    # Check for tax year in both Document Identification and Financial Information sections
    financial_info = extracted_data.get('Financial Information') or {}
    tax_year = get_value_ignore_case(financial_info.get('Tax Year'), 'value')

    if not tax_year:
        print("validation Failed: Tax Year Not Exists")
        return False, "Tax year not found in extracted data"

    # Clean and validate tax year
    if isinstance(tax_year, dict) and 'value' in tax_year:
        tax_year = tax_year['value']

    # Extract year from string if necessary
    if isinstance(tax_year, str):
        # Remove any non-numeric characters
        tax_year = ''.join(filter(str.isdigit, tax_year))

    try:
        tax_year = int(tax_year)
        if tax_year != datetime.now().year - 2:
            print("validation Failed: Invalid tax year")
            return False, f"Invalid tax year: {tax_year}"
    except ValueError:
        print("validation Failed: Invalid tax year format")
        return False, f"Invalid tax year format: {tax_year}"
    try:
        user = User.query.filter_by(username=login_username).first()
        print("USER Info:", user)
        if not user:
            print("Validation Failed: User information not found in database")
            return False, "User information not found in database"

        # Extract recipient/employee name
        recipient_info = extracted_data.get('Recipient/Employee Information') or {}
        recipient_name = get_value_ignore_case(recipient_info, 'Name')
        print("Recipient Info:", recipient_name)

        if not recipient_name:
            print("Validation Failed: Recipient/Employee name not found in document")
            return False, "Recipient/Employee name not found in document"

        # Validate names
        is_valid, message = validate_document_names(user, recipient_name, extracted_data)
        if not is_valid:
            return False, message

        print("Name validation successful")
        print(doc_type, doc_type1)

        formType = doc_type.get('value', '') if isinstance(doc_type, dict) else str(doc_type)
        print(f"Comparing document types - Extracted: {formType}, Expected: {doc_type1}")

        # Normalize both document types
        normalized_form_type = normalize_document_type(formType)
        normalized_expected_type = normalize_document_type(doc_type1)

        print(f"Normalized types - Extracted: {normalized_form_type}, Expected: {normalized_expected_type}")

        if normalized_form_type != normalized_expected_type:
            print(f"❌ Document type mismatch - Got: {normalized_form_type}, Expected: {normalized_expected_type}")
            return False, f"Incorrect document type. Please upload a {doc_type1}."
        # formType = doc_type['value']
        # print("Document type",formType,doc_type1)
        # if formType.lower() != doc_type1.lower():
        #     print("❌ Document type mismatch")
        #     return False, f"Incorrect document type. Please upload a {doc_type} ."

        if '1099' in doc_type:
            print("validate data:9")
            return validate_1099(extracted_data)
        elif 'W-2' in doc_type:
            print("\n=== Starting W-2-specific validation ===")
            return validate_w2(extracted_data)
        elif '1098' in doc_type:
            print("validate data:11")
            return validate_1098(extracted_data)
        else:
            # For other document types, we'll just check if financial information is present
            if not financial_info:
                print("validate data:12")
                return False, f"Missing financial information for document type: {doc_type}"
            print("validation Successful")
            return True, f"Basic validation successful for document type: {doc_type}"
    except Exception as e:
        print("Validation Failed: Database error while fetching user information")
        return False, f"Database error while fetching user information: {str(e)}"


def validate_name(name, username):
    if not name or not username:
        print("name:", name, username)
        return False
    return name.lower().replace(" ", "") == username.lower().replace(" ", "")


def validate_id_number(id_number):
    if not id_number:
        return False
    # This is a basic check. Adjust as needed for your specific requirements.
    return re.match(r'^\d{2}-\d{7}$', id_number) is not None or re.match(r'^\d{3}-\d{2}-\d{4}$',
                                                                         id_number) is not None or '*' in id_number


def validate_1099(data):
    financial_info = data.get('Financial Information', {})
    if not financial_info:
        return False, "Missing financial information for 1099 form"
    # Add more specific 1099 validations here if needed
    return True, "1099 data validation successful"


def validate_w2(data):
    required_fields = ['Wages, tips, other compensation', 'Federal income tax withheld']
    financial_info = data.get('Financial Information', {})
    for field in required_fields:
        if field not in financial_info:
            return False, f"Missing required field for W-2: {field}"
    print("\n=== SuccessFull W-2-specific validation ===")
    return True, "W-2 data validation successful"


def validate_1098(data):
    required_fields = ['Mortgage interest received']
    financial_info = data.get('Financial Information', {})
    for field in required_fields:
        if field not in financial_info:
            return False, f"Missing required field for 1098: {field}"
    return True, "1098 data validation successful"


# Tax Document Information Extraction

prompt = config['PROMPTS']['UPLOAD_PROMPT']['template']

Form1040_prompt = config['PROMPTS']['FORM1040_PROMPT']['template']


@app.route('/')
def home():
    return "Welcome to the Flask API", 200


# Add these routes to your Flask application (30-10-2024.py)
@app.route('/api/questionnaire/form-1040', methods=['GET'])
@declaration_token_required
def get_form_1040_questions(user):
    try:
        print("Fetching questionnaire data...")

        # Query categories
        categories = QuestionCategory.query.filter_by(active=True).order_by(QuestionCategory.display_order).all()
        if not categories:
            app.logger.warning("No active categories found.")
            return jsonify({'status': 'error', 'message': 'No categories found'}), 404

        formatted_questions = []

        for category in categories:
            # Query questions for the category
            questions = TaxQuestion.query.filter(
                TaxQuestion.category_id == category.id,
                TaxQuestion.active == True
            ).order_by(TaxQuestion.display_order).all()

            if not questions:
                app.logger.warning(f"No active questions found for category {category.id}.")
                continue

            for question in questions:
                # Get document mappings for the question
                doc_mappings = QuestionDocumentMapping.query.filter_by(
                    question_id=question.id
                ).all()

                # Retrieve required document types
                required_docs = []
                for mapping in doc_mappings:
                    doc_type = DocumentType.query.get(mapping.document_type_id)
                    if doc_type:
                        required_docs.append({
                            'document_type': doc_type.type_name,
                            'description': doc_type.description,
                            'required': mapping.required,
                            'priority': mapping.priority,
                            'response_trigger': mapping.response_trigger
                        })
                    else:
                        app.logger.warning(f"Document type not found for mapping {mapping.id}")

                # Append question with its details
                formatted_questions.append({
                    'category_id': category.id,
                    'category_name': category.name,
                    'category_description': category.description,
                    'question_id': question.id,
                    'question_text': question.question_text,
                    'question_type': question.question_type,
                    'help_text': question.help_text,
                    'required': question.required,
                    'required_documents': required_docs
                })

        print(f"Returning {len(formatted_questions)} questions")

        return jsonify({
            'status': 'success',
            'questions': formatted_questions
        }), 200

    except Exception as e:
        print(f"Error fetching Form 1040 questions: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f'Failed to fetch questions: {str(e)}'
        }), 500


@app.route('/api/questionnaire/save-progress', methods=['POST'])
@declaration_token_required
def save_progress(user):
    try:
        data = request.get_json()
        section_name = data.get('section')
        answers = data.get('answers')
        follow_up_answers = data.get('followUpAnswers', {})

        if not answers:
            return jsonify({"status": "error", "message": "No answers provided"}), 400

        # Get the customer record for this user
        customer = Customer.query.filter_by(user_id=user.user_id).first()
        if not customer:
            return jsonify({
                "status": "error",
                "message": "Customer record not found for this user"
            }), 404

        tenant_id = user.tenant_id
        tax_year = 2024
        current_time = datetime.now(timezone.utc)

        for question_id, answer_data in answers.items():
            # Prepare the response value
            response_dict = {
                'value': answer_data.get('value'),
                'documents': answer_data.get('documents', [])
            }

            # Add follow-up answers if they exist
            if question_id in follow_up_answers:
                response_dict['followUp'] = follow_up_answers[question_id]

            # Convert to JSON string
            response_value = json.dumps(response_dict)

            # Additional notes can be derived from section name or other metadata
            additional_notes = f"Section: {section_name}"

            # Check if response already exists
            existing_response = CustomerResponse.query.filter_by(
                user_id=user.user_id,
                tax_year=tax_year,
                question_id=int(question_id)
            ).first()

            if existing_response:
                # Update existing response
                existing_response.response_value = response_value
                existing_response.additional_notes = additional_notes
                existing_response.last_modified_by = user.user_id
                existing_response.last_modified_date = current_time
            else:
                # Create new response with all fields
                new_response = CustomerResponse(
                    user_id=user.user_id,
                    customer_id=customer.id,
                    tenant_id=tenant_id,
                    tax_year=tax_year,
                    question_id=int(question_id),
                    response_value=response_value,
                    additional_notes=additional_notes,
                    created_by=user.user_id,
                    created_date=current_time,
                    last_modified_by=user.user_id,
                    last_modified_date=current_time
                )
                db.session.add(new_response)

        db.session.commit()
        return jsonify({
            "status": "success",
            "message": "Responses saved successfully"
        }), 200

    except Exception as e:
        db.session.rollback()
        print(f"Error saving responses: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route('/api/questionnaire/progress/<token>', methods=['GET'])
@declaration_token_required
def get_questionnaire_progress(user):
    try:
        responses = CustomerResponse.query.filter_by(
            customer_id=user.user_id
        ).all()

        formatted_responses = {}
        for response in responses:
            value = response.response_value
            # Try to parse JSON if it's a string representation of an array
            try:
                if value.startswith('['):
                    value = json.loads(value)
            except (json.JSONDecodeError, AttributeError):
                pass

            formatted_responses[str(response.question_id)] = value

        return jsonify({
            'status': 'success',
            'answers': formatted_responses
        }), 200

    except Exception as e:
        print(f"Error fetching questionnaire progress: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/questionnaire/submit', methods=['POST'])
@declaration_token_required
def submit_questionnaire(user):
    try:
        data = request.get_json()
        answers = data.get('answers', {})
        follow_up_answers = data.get('followUpAnswers', {})

        # Save final answers
        for question_id, answer in answers.items():
            # Convert array response to JSON string if it's a list
            if isinstance(answer, list):
                answer = json.dumps(answer)

            response = CustomerResponse(
                user_id=user.user_id,
                tax_year=2024,
                question_id=int(question_id),
                response_value=answer,
                created_by=user.user_id,
                last_modified_by=user.user_id
            )
            db.session.add(response)

        # Save follow-up answers
        for question_id, follow_up_data in follow_up_answers.items():
            response = CustomerResponse(
                user_id=user.user_id,
                tax_year=2024,
                question_id=int(question_id),
                response_value=json.dumps(follow_up_data),
                additional_notes='follow_up_answer',
                created_by=user.user_id,
                last_modified_by=user.user_id
            )
            db.session.add(response)

        db.session.commit()
        return jsonify({
            "status": "success",
            "message": "Questionnaire submitted successfully"
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route('/api/documents/status', methods=['GET'])
@token_required
def get_customer_document_status(current_user):
    try:
        # Query to get customer details with document counts
        query = db.session.query(
            Customer.id.label('customer_id'),  # Get customer ID
            func.concat(User.first_name, ' ', User.last_name).label('customer_name'),
            func.count(Document.document_id).label('total_docs'),
            func.sum(case(
                (Document.status == 'SUBMITTED', 1),
                else_=0
            )).label('uploaded_docs'),
            func.sum(case(
                (Document.status == 'active', 1),
                else_=0
            )).label('pending_docs'),
            func.max(Document.due_date).label('due_date')
        ).join(
            Document, Customer.id == Document.customer_id
        ).join(
            DocumentType, Document.document_type_id == DocumentType.document_type_id
        ).join(
            User, Customer.user_id == User.user_id
        ).filter(
            DocumentType.document_type_id != 48
        ).group_by(
            Customer.id,
            User.first_name,
            User.last_name
        ).all()

        # Convert query results to list of dictionaries
        customers_list = []
        for result in query:
            customer_data = {
                'customer_id': result.customer_id,  # Include customer_id in the response
                'customer_name': result.customer_name or 'Unknown',
                'total_docs': int(result.total_docs or 0),
                'uploaded_docs': int(result.uploaded_docs or 0),
                'pending_docs': int(result.pending_docs or 0),
                'due_date': result.due_date.strftime('%Y-%m-%d') if result.due_date else None
            }

            # Verify that uploaded + pending = total
            total_calculated = customer_data['uploaded_docs'] + customer_data['pending_docs']
            if total_calculated != customer_data['total_docs']:
                # Adjust pending docs to make the total match if necessary
                customer_data['pending_docs'] = customer_data['total_docs'] - customer_data['uploaded_docs']

            customers_list.append(customer_data)

        # Sort the list by due date (None values last)
        customers_list.sort(
            key=lambda x: (x['due_date'] is None, x['due_date'] or '9999-12-31')
        )

        # Print debug information
        print("Response data:", customers_list)

        return jsonify({
            'status': 'success',
            'data': customers_list,
            'summary': {
                'total_customers': len(customers_list),
                'total_documents': sum(c['total_docs'] for c in customers_list),
                'total_pending': sum(c['pending_docs'] for c in customers_list),
                'total_uploaded': sum(c['uploaded_docs'] for c in customers_list)
            }
        })

    except Exception as e:
        print(f"Error in get_customer_document_status: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/request_document_deletion', methods=['POST'])
@token_required
def request_document_deletion(current_user):
    try:
        # Get request data
        data = request.get_json()
        document_id = data.get('document_id')
        reason = data.get('reason')
        comments = data.get('comments')
        print("Obtained Data:\n", data)

        if not document_id or not reason:
            return jsonify({
                'status': 'error',
                'message': 'Document ID and reason are required'
            }), 400

        # Find the initial document to get document_type_id
        initial_document = Document.query.get(document_id)
        if not initial_document:
            return jsonify({
                'status': 'error',
                'message': 'Document not found'
            }), 404

        # Get customer record
        customer = Customer.query.filter_by(user_id=current_user.user_id).first()
        if not customer:
            return jsonify({
                'status': 'error',
                'message': 'Customer record not found'
            }), 404

        # Find all documents of the same type for this customer
        documents_to_delete = Document.query.filter_by(
            customer_id=customer.id,
            document_type_id=initial_document.document_type_id,
            tax_year=initial_document.tax_year,
            is_deleted=False  # Only get non-deleted documents
        ).all()

        if not documents_to_delete:
            return jsonify({
                'status': 'error',
                'message': 'No active documents found to delete'
            }), 404

        # Update all found documents
        current_time = datetime.now(timezone.utc)
        for doc in documents_to_delete:
            doc.is_deleted = True
            doc.del_reason_code = reason
            doc.delete_comments = comments
            doc.status = 'DELETED'
            doc.last_modified_by = current_user.user_id
            doc.updated_at = current_time

        db.session.commit()

        return jsonify({
            'status': 'success',
            'message': f'Successfully deleted {len(documents_to_delete)} documents',
            'document_id': document_id,
            'deleted_count': len(documents_to_delete)
        }), 200

    except Exception as e:
        db.session.rollback()
        print(f"Error in document deletion: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f'Failed to process deletion request: {str(e)}'
        }), 500


@app.route('/api/documents/<int:document_id>/due-date-message', methods=['POST'])
@token_required
def add_document_message(current_user, document_id):
    try:
        data = request.get_json()
        message = data.get('message')
        user_id = data.get('customer_id')
        new_due_date = data.get('newDueDate')

        if not message:
            return jsonify({'error': 'Message is required'}), 400

        print("Provide data:", data)
        customers = Customer.query.filter_by(user_id=user_id).first()
        print("Customers:", customers)
        # Get the document
        document = Document.query.get(document_id)
        if not document:
            return jsonify({'error': 'Document not found'}), 404

        print("document found:", document.document_id)
        print("Current User:", current_user)

        try:
            # Create new document message
            new_message = DocumentMessages(
                document_id=document.document_id,
                customer_id=customers.id,
                message=message,
                created_by=current_user.user_id,
                created_at=datetime.now(timezone.utc),
                last_modified_by=current_user.user_id,
                updated_at=datetime.now(timezone.utc)
            )
            # If a new due date is provided, update the document's due date
            if new_due_date:
                try:
                    new_due_date = datetime.fromisoformat(new_due_date.replace('Z', '+00:00'))
                    document.customer_entered_due_date = new_due_date
                    document.customer_due_date_comments = message
                except Exception as date_error:
                    print(f"Error parsing due date: {date_error}")
                    return jsonify({
                        'error': 'Invalid date format',
                        'details': str(date_error)
                    }), 400
            else:
                try:
                    document.customer_due_date_comments = message
                except Exception as date_error:
                    print(f"Error parsing due date: {date_error}")
                    return jsonify({
                        'error': 'Invalid date format',
                        'details': str(date_error)
                    }), 400
            db.session.add(new_message)
            db.session.commit()

            return jsonify({
                'message': 'Due date update request submitted successfully',
                'document_message_id': new_message.id
            }), 201

        except Exception as db_error:
            db.session.rollback()
            print(f"Database Error: {str(db_error)}")
            print(f"Error Type: {type(db_error)}")
            import traceback
            traceback.print_exc()  # This will print the full stack trace
            return jsonify({
                'error': 'Failed to create document message',
                'details': str(db_error)
            }), 500

    except Exception as e:
        print(f"General Error: {str(e)}")
        print(f"Error Type: {type(e)}")
        import traceback
        traceback.print_exc()  # This will print the full stack trace
        return jsonify({'error': str(e)}), 500
@app.route('/document_types/categories', methods=['GET'])
@token_required
def get_document_categories(current_user):
    """Get all categories with their available document types"""
    try:
        # Get unique categories
        categories = {}
        doc_types = DocumentType.query.order_by(
            DocumentType.category_name,
            DocumentType.type_name
        ).all()

        for doc in doc_types:
            if doc.category_name:
                if doc.category_name not in categories:
                    categories[doc.category_name] = []

                categories[doc.category_name].append({
                    'document_type_id': doc.document_type_id,
                    'type_name': doc.type_name,
                    'description': doc.description
                })

        return jsonify({
            'categories': categories
        }), 200

    except Exception as e:
        print(f"Error fetching categories: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/document_types', methods=['GET'])
@token_required
def get_document_types(current_user):
    """Get document types for a specific category"""
    try:
        category = request.args.get('category')
        if not category:
            return jsonify({'error': 'Category parameter is required'}), 400

        doc_types = DocumentType.query.filter(
            func.trim(DocumentType.category_name) == category.strip()
        ).order_by(DocumentType.type_name).all()

        formatted_types = [{
            'document_type_id': doc.document_type_id,
            'type_name': doc.type_name,
            'description': doc.description
        } for doc in doc_types]

        return jsonify(formatted_types), 200

    except Exception as e:
        print(f"Error fetching document types: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/add_document', methods=['POST'])
@token_required
def add_document(current_user):
    """Add a document record for the customer"""
    try:
        data = request.get_json()
        document_type_id = data.get('document_type_id')
        tax_year = data.get('tax_year', datetime.now().year - 1)  # Default to previous year

        if not document_type_id:
            return jsonify({'error': 'Missing required fields'}), 400

        # Get customer record
        customer = Customer.query.filter_by(user_id=current_user.user_id).first()
        if not customer:
            return jsonify({"error": "Customer record not found"}), 404

        # Get document type details
        doc_type = DocumentType.query.get(document_type_id)
        if not doc_type:
            return jsonify({"error": "Invalid document type"}), 400

        # Create new document record
        new_document = Document(
            customer_id=customer.id,
            document_type_id=document_type_id,
            file_name='additional',
            file_path='New_file_path',
            file_size=0,
            mime_type='pdf/png',
            tax_year=tax_year,
            status='active',
            created_by=current_user.user_id,
            created_at=datetime.now(timezone.utc),
            last_modified_by=current_user.user_id,
            process_status='PENDING',
            is_deleted=False,
            tenant_id=1,
            requirement_source=doc_type.type_name,
            due_date=datetime.now(timezone.utc) + timedelta(days=30)
        )

        db.session.add(new_document)
        db.session.commit()

        return jsonify({
            'status': 'success',
            'message': 'Document added successfully',
            'document_id': new_document.document_id
        }), 200

    except Exception as e:
        db.session.rollback()
        print(f"Error adding document: {str(e)}")
        return jsonify({'error': str(e)}), 500




# notification alert system
class DocumentNotificationService:
    def __init__(self, app):
        self.app = app
        self.logger = app.logger
        self.mail = mail  # Using your existing Flask-Mail instance
        self.is_processing = False # Add processing flag

    def get_active_triggers(self):
        """Retrieve all active notification triggers"""
        try:
            print("Fetching all active notification triggers...")
            triggers = NotificationTriggers.query.filter_by(active=True).all()

            print(f"Found {len(triggers)} active triggers")
            for t in triggers:
                print(f"Trigger ID: {t.id}, Type: {t.trigger_type}, "f"Entity ID: {t.trigger_entity_id}")
            return triggers

        except Exception as e:
            print(f"Error fetching triggers: {e}")
            return []
    def get_notification_type(self, type_id):
        """Get notification type details"""
        try:
            return db.session.get(NotificationTypes, type_id)
        except Exception as e:
            print(f"Error fetching notification type {type_id}: {e}")
            return None
    def get_customers_with_pending_documents(self, trigger, current_date):
        """Get customers with documents due within the trigger's offset range"""
        try:
            print(f"Finding customers for trigger {trigger.id}")

            # Calculate date range based on offset range
            range_start = current_date + timedelta(days=trigger.days_offset_start)
            range_end = current_date + timedelta(days=trigger.days_offset_end)

            print(f"Checking for documents due between {range_start.date()} and {range_end.date()}")

            # Use func.date() to compare only the date part
            query = (
                db.session.query(Customer)
                .join(Document, Document.customer_id == Customer.id)
                .join(DocumentType, Document.document_type_id == DocumentType.document_type_id)
                .filter(
                    Document.status == 'active',
                    func.date(Document.due_date).between(
                        range_start.date(),
                        range_end.date()
                    )
                )
                .distinct()
            )

            customers = query.all()
            # print(f"Found {len(customers)} customers with documents due in range")

            # Store document info for email content
            self.customer_documents = defaultdict(list)

            for customer in customers:
                # Get all active documents for this customer due within range
                docs = (Document.query
                        .filter(
                    Document.customer_id == customer.id,
                    Document.status == 'active',
                    func.date(Document.due_date).between(
                        range_start.date(),
                        range_end.date()
                    )
                )
                        .all())

                if docs:
                    # print(f"\nCustomer {customer.id} documents due in range:")
                    for doc in docs:
                        doc_type = db.session.get(DocumentType, doc.document_type_id)
                        if doc_type:
                            # Convert both dates to date objects for comparison
                            due_date = doc.due_date.date() if isinstance(doc.due_date, datetime) else doc.due_date
                            current = current_date.date() if isinstance(current_date, datetime) else current_date
                            days_until_due = (due_date - current).days

                            # print(f"- {doc_type.type_name} (Due in {days_until_due} days)")
                            self.customer_documents[customer.id].append({
                                'type_name': doc_type.type_name,
                                'due_date': doc.due_date,
                                'document_id': doc.document_id,
                                'days_until_due': days_until_due
                            })

            return customers

        except Exception as e:
            print(f"Error getting customers for trigger: {e}")
            traceback.print_exc()
            return []
    def create_notification(self, customer, notification_type, trigger):
        """Create a new notification record"""
        try:
            notification = CustomerNotifications(
                customer_id=customer.id,
                notification_type_id=notification_type.id,
                subject=notification_type.template_subject,
                message=notification_type.template_body,
                priority=notification_type.priority,
                status='PENDING',
                related_entity_type=trigger.trigger_entity_type,
                related_entity_id=trigger.trigger_entity_id,
                due_date=datetime.now(timezone.utc) + timedelta(days=trigger.days_offset),
                created_by=1,
                created_date=datetime.now(timezone.utc),
                last_modified_by=1,
                last_modified_date=datetime.now(timezone.utc)
            )

            db.session.add(notification)
            db.session.commit()

            print(f"Created notification {notification.id} for customer {customer.id}")
            return notification.id

        except Exception as e:
            print(f"Error creating notification: {e}")
            db.session.rollback()
            return None
    def record_delivery(self, notification_id, success):
        """Record the notification delivery attempt"""
        try:
            delivery = NotificationDeliveries(
                notification_id=notification_id,
                channel='EMAIL',
                status='SENT' if success else 'FAILED',
                sent_date=datetime.now(timezone.utc) if success else None,
                error_message=None if success else 'Failed to send email',
                created_by=1,
                created_date=datetime.now(timezone.utc),
                last_modified_by=1,
                last_modified_date=datetime.now(timezone.utc)
            )

            db.session.add(delivery)
            db.session.commit()
            print(f"Recorded delivery status for notification {notification_id}")

        except Exception as e:
            print(f"Error recording delivery: {e}")
            db.session.rollback()
    @app.route('/api/documents/send-notification/<int:document_id>', methods=['POST'])
    @token_required
    def send_document_notification(current_user, document_id):
        try:
            # Get the document
            document = Document.query.get(document_id)
            if not document:
                return jsonify({"error": "Document not found"}), 404

            # Get customer details
            customer = Customer.query.get(document.customer_id)
            if not customer:
                return jsonify({"error": "Customer not found"}), 404

            # Get user details for email
            user = User.query.get(customer.user_id)
            if not user or not user.email:
                return jsonify({"error": "Customer email not found"}), 404

            # Get document type
            doc_type = DocumentType.query.get(document.document_type_id)
            if not doc_type:
                return jsonify({"error": "Document type not found"}), 404


            templates = load_email_templates()
            template = templates['DOCUMENT']['SINGLE_NOTIFICATION']
            template_data = {
                'customer_name': f"{user.first_name} {user.last_name}",
                'document_type': doc_type.type_name,
                'due_date': document.due_date.strftime('%B %d, %Y') if document.due_date else "Not set",
                'portal_url': app.config['FRONTEND_URL']
            }

            # Create notification record
            notification = CustomerNotifications(
                customer_id=customer.id,
                document_id=document.document_id,
                notification_type_id=2,  # Assuming 1 is for document reminders
                subject=template['SUBJECT'].format(**template_data),
                message=template['HTML'].format(**template_data),
                priority="HIGH",
                status="PENDING",
                related_entity_type="DOCUMENT",
                related_entity_id=document.document_id,
                due_date=document.due_date,
                created_by=current_user.user_id,
                created_date=datetime.now(timezone.utc),
                last_modified_by=current_user.user_id,
                last_modified_date=datetime.now(timezone.utc)
            )

            db.session.add(notification)
            db.session.flush()  # Get notification ID

            # Create notification item
            notification_item = CustomerNotificationItem(
                customer_notification_id=notification.id,
                customer_id=customer.id,
                document_id=document.document_id,
                created_by=current_user.user_id,
                created_date=datetime.now(timezone.utc),
                last_modified_by=current_user.user_id,
                last_modified_date=datetime.now(timezone.utc)
            )

            db.session.add(notification_item)

            # Send email
            msg = Message(
                subject=notification.subject,
                recipients=[user.email],
                html=notification.message,
                sender=app.config['MAIL_DEFAULT_SENDER']
            )
            mail.send(msg)

            # Record successful delivery
            delivery = NotificationDeliveries(
                notification_id=notification.id,
                channel='EMAIL',
                status='SENT',
                sent_date=datetime.now(timezone.utc),
                created_by=current_user.user_id,
                created_date=datetime.now(timezone.utc),
                last_modified_by=current_user.user_id,
                last_modified_date=datetime.now(timezone.utc)
            )

            db.session.add(delivery)

            # Update notification status
            notification.status = 'SENT'

            db.session.commit()

            return jsonify({
                "message": f"Notification sent successfully to {user.email}",
                "notification_id": notification.id
            }), 200

        except Exception as e:
            db.session.rollback()
            print(f"Error sending notification: {str(e)}")
            return jsonify({
                "error": f"Failed to send notification: {str(e)}"
            }), 500
    @app.route('/api/documents/send-all-notifications/<int:customer_id>', methods=['POST'])
    @token_required
    def send_all_document_notifications(current_user, customer_id):
        try:
            # Get the customer
            customer = Customer.query.get(customer_id)
            if not customer:
                return jsonify({"error": "Customer not found"}), 404

            # Get the user for email
            user = User.query.get(customer.user_id)
            if not user or not user.email:
                return jsonify({"error": "Customer email not found"}), 404

            # Get all pending documents for this customer
            pending_documents = (db.session.query(Document, DocumentType)
                                 .join(DocumentType, Document.document_type_id == DocumentType.document_type_id)
                                 .filter(
                Document.customer_id == customer.id,
                Document.status == 'active'
            ).all())

            if not pending_documents:
                return jsonify({"message": "No pending documents found"}), 200
            templates = load_email_templates()
            template = templates['DOCUMENT']['BULK_NOTIFICATION']
            # Create document list for email
            document_list= "\n".join([
                f'<li>{doc_type.type_name} (Due Date: {doc.due_date.strftime("%B %d, %Y") if doc.due_date else "Not set"})</li>'
                for doc, doc_type in pending_documents
            ])

            template_data = {
                'customer_name': f"{user.first_name} {user.last_name}",
                'document_list': document_list,
                'portal_url': app.config['FRONTEND_URL']
            }

            # Format email content using template
            html_content = template['HTML'].format(**template_data)
            subject = template['SUBJECT']

            # Create notification record
            notification = CustomerNotifications(
                customer_id=customer.id,
                notification_type_id=2,  # Assuming 2 is for bulk document reminders
                subject=subject,
                message=html_content,
                priority="HIGH",
                status="PENDING",
                related_entity_type="DOCUMENT",
                created_by=current_user.user_id,
                created_date=datetime.now(timezone.utc),
                last_modified_by=current_user.user_id,
                last_modified_date=datetime.now(timezone.utc)
            )

            db.session.add(notification)
            db.session.flush()

            # Create notification items for each document
            for doc, _ in pending_documents:
                notification_item = CustomerNotificationItem(
                    customer_notification_id=notification.id,
                    customer_id=customer.id,
                    document_id=doc.document_id,
                    created_by=current_user.user_id,
                    created_date=datetime.now(timezone.utc),
                    last_modified_by=current_user.user_id,
                    last_modified_date=datetime.now(timezone.utc)
                )
                db.session.add(notification_item)

            # Send email
            msg = Message(
                subject=notification.subject,
                recipients=[user.email],
                html=html_content,
                sender=app.config['MAIL_DEFAULT_SENDER']
            )
            mail.send(msg)

            # Record successful delivery
            delivery = NotificationDeliveries(
                notification_id=notification.id,
                channel='EMAIL',
                status='SENT',
                sent_date=datetime.now(timezone.utc),
                created_by=current_user.user_id,
                created_date=datetime.now(timezone.utc),
                last_modified_by=current_user.user_id,
                last_modified_date=datetime.now(timezone.utc)
            )

            db.session.add(delivery)

            # Update notification status
            notification.status = 'SENT'

            db.session.commit()

            return jsonify({
                "message": f"Notification sent successfully to {user.email}",
                "notification_id": notification.id,
                "documents_included": len(pending_documents)
            }), 200

        except Exception as e:
            db.session.rollback()
            print(f"Error sending notifications: {str(e)}")
            return jsonify({
                "error": f"Failed to send notifications: {str(e)}"
            }), 500
    @app.route('/api/debug/check-due-dates', methods=['GET'])
    @token_required
    def debug_due_dates(current_user):
        try:
            current_date = datetime.now(timezone.utc)
            print(f"\nCurrent date: {current_date}")

            # Query all documents due today
            docs = (Document.query
                    .join(Customer)
                    .join(DocumentType)
                    .filter(
                Document.status == 'active',
                func.date(Document.due_date) == current_date.date()
            )
                    .all())

            debug_info = {
                'current_date': current_date.isoformat(),
                'documents_found': len(docs),
                'documents': [{
                    'document_id': doc.document_id,
                    'customer_id': doc.customer_id,
                    'due_date': doc.due_date.isoformat() if doc.due_date else None,
                    'status': doc.status,
                    'document_type_id': doc.document_type_id
                } for doc in docs]
            }

            return jsonify(debug_info), 200

        except Exception as e:
            return jsonify({'error': str(e)}), 500
    def send_formatted_email(self, user, customer, documents, trigger, current_date,tax_year_str):
        """
        Send formatted email notification to user with document information

        Args:
            user: User object containing recipient details
            customer: Customer object
            documents: List of document dictionaries
            trigger: NotificationTriggers object
            tax_year_str: String representing tax year(s)
            current_date: Current datetime

        Returns:
            tuple: (success: bool, notification: CustomerNotifications)
        """
        try:
            templates = load_email_templates()
            template = templates['DOCUMENT']['SYSTEM_NOTIFICATION']
            # Create document list
            document_list = "\n".join([
                f'<li>{doc["type_name"]} (Due Date: {doc["due_date"].strftime("%B %d, %Y")})</li>'
                for doc in documents
            ])

            # Format template variables
            template_data = {
                'customer_name': f"{user.first_name} {user.last_name}",
                'tax_year': tax_year_str,
                'document_list': document_list,
                'portal_url': self.app.config['FRONTEND_URL']
            }

            # Format email content
            html_message = template['HTML'].format(**template_data)
            subject = template['SUBJECT'].format(**template_data)

            # Create notification record using customer.id instead of user.user_id
            notification = CustomerNotifications(
                customer_id=customer.id,
                notification_type_id=trigger.notification_type_id,
                subject=subject,
                message=html_message,
                priority='HIGH',
                status='PENDING',
                due_date=current_date + timedelta(days=trigger.days_offset_end),  # Use end date for due date
                created_by=1,
                created_date=current_date,
                last_modified_by=1,
                last_modified_date=current_date
            )

            # Create and send email
            msg = Message(
                subject=notification.subject,
                recipients=[user.email],
                html=html_message,
                sender=self.app.config['MAIL_DEFAULT_SENDER']
            )

            self.mail.send(msg)
            print(f"Sent email to {user.email}")

            return True, notification

        except Exception as e:
            print(f"Error sending email: {str(e)}")
            print(f"Traceback: {traceback.format_exc()}")
            return False, None
    def process_notifications(self):
        """Main notification processing method with date range support"""
        with self.app.app_context():
            try:
                print("Starting notification processing")
                notified_customers = defaultdict(set)
                current_date = datetime.now(timezone.utc)

                triggers = NotificationTriggers.query.filter_by(active=True).all()
                print(f"Found {len(triggers)} active triggers")

                for trigger in triggers:
                    try:
                        # print(f"\nProcessing trigger {trigger.id}")
                        # print(f"Offset range: {trigger.days_offset_start} to {trigger.days_offset_end} days")

                        notification_type = self.get_notification_type(trigger.notification_type_id)
                        if not notification_type:
                            continue

                        customers = self.get_customers_with_pending_documents(trigger, current_date)

                        for customer in customers:
                            try:
                                documents = self.customer_documents[customer.id]
                                if not documents:
                                    continue

                                user = User.query.filter_by(user_id=customer.user_id).first()
                                if not user or not user.email:
                                    continue

                                # Group documents by tax year for better organization
                                # Use isinstance to handle both datetime and date objects
                                tax_years = sorted(set(
                                    doc['due_date'].year if isinstance(doc['due_date'], datetime)
                                    else doc['due_date'].year for doc in documents
                                ))
                                tax_year_str = ", ".join(str(year) for year in tax_years)

                                # Send email and create notification
                                success, notification = self.send_formatted_email(
                                    user=user,
                                    customer=customer,
                                    documents=documents,
                                    tax_year_str=tax_year_str,
                                    trigger=trigger,
                                    current_date=current_date
                                )

                                if success and notification:
                                    db.session.add(notification)
                                    db.session.flush()

                                    # Create notification items
                                    for doc in documents:
                                        notification_item = CustomerNotificationItem(
                                            customer_notification_id=notification.id,
                                            customer_id=customer.id,
                                            document_id=doc['document_id'],
                                            created_by=1,
                                            created_date=current_date,
                                            last_modified_by=1,
                                            last_modified_date=current_date
                                        )
                                        db.session.add(notification_item)

                                    # Record delivery
                                    delivery = NotificationDeliveries(
                                        notification_id=notification.id,
                                        channel='EMAIL',
                                        status='SENT',
                                        sent_date=current_date,
                                        created_by=1,
                                        created_date=current_date,
                                        last_modified_by=1,
                                        last_modified_date=current_date
                                    )
                                    db.session.add(delivery)

                                    # Handle both datetime and date objects when getting the key
                                    due_date = min(
                                        doc['due_date'] if isinstance(doc['due_date'], date)
                                        else doc['due_date'].date()
                                        for doc in documents
                                    )
                                    notified_customers[due_date].add(customer.id)
                                    db.session.commit()
                                    print(f"Processed notification for customer {customer.id}")

                            except Exception as e:
                                db.session.rollback()
                                print(f"Error processing customer {customer.id}: {str(e)}")
                                print(f"Traceback: {traceback.format_exc()}")

                    except Exception as e:
                        print(f"Error processing trigger {trigger.id}: {str(e)}")
                        print(f"Traceback: {traceback.format_exc()}")

                print("Completed notification processing")

            except Exception as e:
                print(f"Error in notification processing: {str(e)}")
                print(f"Traceback: {traceback.format_exc()}")
    def verify_notification_items(self, notification_id):
        """Verify notification items were created for a notification"""
        try:
            items = CustomerNotificationItem.query.filter_by(
                customer_notification_id=notification_id
            ).all()
            print(f"Found {len(items)} notification items for notification {notification_id}")

            # Get details of each item for debugging
            for item in items:
                print(f"  - Item {item.customer_notification_item_id}: "
                      f"Document {item.document_id}, Customer {item.customer_id}")

            return len(items) > 0

        except Exception as e:
            print(f"Error verifying notification items: {str(e)}")
            print(f"Traceback: {traceback.format_exc()}")
            return False
# Add a global flag to track if scheduler is running
scheduler_started = False
notification_system = None
def init_scheduler(app):
    global scheduler_started, notification_system

    if not os.environ.get('WERKZEUG_RUN_MAIN'):
        print("### PARENT PROCESS: Skipping scheduler initialization")
        return

    if not scheduler_started:
        print(f"### MAIN PROCESS: Initializing scheduler at {datetime.now()}")
        notification_system = DocumentNotificationService(app)
        scheduler = BackgroundScheduler()

        # Set up 24-hours interval
        scheduler.add_job(
            notification_system.process_notifications,
            'interval',
            hours=24,  # Run every 24 hours
            id='notification_job',
            name='Recurring Notification Job',
            next_run_time=datetime.now()  # Start first run immediately
        )

        if not scheduler.running:
            scheduler.start()
            scheduler_started = True
            print(f"### MAIN PROCESS: Scheduler started successfully")
            print(f"### MAIN PROCESS: Next notification will run in 30 minutes")
    else:
        print("### MAIN PROCESS: Scheduler already running")
@app.route('/api/trigger-notifications', methods=['POST'])
@token_required
def trigger_notifications(current_user):
    global notification_system
    """Manually trigger notification processing (admin only)"""
    if not current_user.is_admin:
        return jsonify({"error": "Unauthorized"}), 403

    try:
        notification_system.process_notifications()
        return jsonify({
            "message": "Notifications processed successfully"
        }), 200
    except Exception as e:
        return jsonify({
            "error": f"Failed to process notifications: {str(e)}"
        }), 500
@app.route('/api/check-active-documents', methods=['GET'])
@token_required
def check_active_documents(current_user):
    try:
        print("\n=== Checking Active Documents ===")

        # Query active documents with customer and document type info
        active_docs = (db.session.query(
            Document, Customer, DocumentType, User
        )
                       .join(Customer, Document.customer_id == Customer.id)
                       .join(DocumentType, Document.document_type_id == DocumentType.document_type_id)
                       .join(User, Customer.user_id == User.user_id)
                       .filter(Document.status == 'active')
                       .all())

        print(f"\nFound {len(active_docs)} active documents")

        formatted_results = []
        for doc, customer, doc_type, user in active_docs:
            doc_info = {
                'document_id': doc.document_id,
                'customer_id': customer.id,
                'customer_name': f"{user.first_name} {user.last_name}",
                'customer_email': user.email,
                'document_type': doc_type.type_name,
                'status': doc.status,
                'due_date': doc.due_date.isoformat() if doc.due_date else None,
                'upload_date': doc.upload_date.isoformat() if doc.upload_date else None
            }
            print(f"\nDocument Details:")
            print(f"Document ID: {doc.document_id}")
            print(f"Customer: {user.first_name} {user.last_name}")
            print(f"Document Type: {doc_type.type_name}")
            print(f"Status: {doc.status}")
            print(f"Due Date: {doc.due_date}")

            formatted_results.append(doc_info)

        return jsonify({
            'total_documents': len(active_docs),
            'documents': formatted_results
        }), 200

    except Exception as e:
        print(f"Error checking active documents: {str(e)}")
        return jsonify({'error': str(e)}), 500
# Add this function to check notification triggers
@app.route('/api/check-notification-triggers', methods=['GET'])
@token_required
def check_notification_triggers(current_user):
    try:
        print("\n=== Checking Notification Triggers ===")

        # Get active triggers
        triggers = NotificationTriggers.query.filter_by(active=True).all()
        print(f"\nFound {len(triggers)} active triggers")

        trigger_details = []
        for trigger in triggers:
            # Get notification type
            notification_type = NotificationTypes.query.get(trigger.notification_type_id)

            # Get matching documents
            if trigger.trigger_type == 'DOCUMENT':
                matching_docs = Document.query.filter(
                    Document.document_type_id == trigger.trigger_entity_id,
                    Document.status == 'active'
                ).all()

                print(f"\nTrigger ID: {trigger.id}")
                print(f"Trigger Type: {trigger.trigger_type}")
                print(f"Entity ID: {trigger.trigger_entity_id}")
                print(f"Matching Documents: {len(matching_docs)}")

                # Get unique customers for these documents
                customer_ids = set(doc.customer_id for doc in matching_docs)
                customers = Customer.query.filter(Customer.id.in_(customer_ids)).all()

                trigger_info = {
                    'trigger_id': trigger.id,
                    'trigger_type': trigger.trigger_type,
                    'notification_type': notification_type.name if notification_type else 'Unknown',
                    'entity_id': trigger.trigger_entity_id,
                    'matching_documents': len(matching_docs),
                    'affected_customers': len(customers),
                    'documents': [{
                        'document_id': doc.document_id,
                        'customer_id': doc.customer_id,
                        'status': doc.status
                    } for doc in matching_docs]
                }
                trigger_details.append(trigger_info)

                print("\nMatching Documents:")
                for doc in matching_docs:
                    print(f"Document ID: {doc.document_id}, Customer ID: {doc.customer_id}, Status: {doc.status}")

        return jsonify({
            'total_triggers': len(triggers),
            'triggers': trigger_details
        }), 200

    except Exception as e:
        print(f"Error checking notification triggers: {str(e)}")
        return jsonify({'error': str(e)}), 500


# support ticket system
def generate_ticket_number(customer_id):
    """
    Generate a ticket number in format TK-XXXNN where:
    XXX = customer_id
    NN = sequential number for that customer
    """
    try:
        # Get the latest ticket number for this customer
        latest_ticket = (Ticket.query
                         .filter(Ticket.ticket_number.like(f'TK-{customer_id}%'))
                         .order_by(Ticket.ticket_number.desc())
                         .first())

        if latest_ticket:
            # Extract the sequence number from the last ticket
            last_sequence = int(latest_ticket.ticket_number[-2:])
            new_sequence = str(last_sequence + 1).zfill(2)  # Pad with zeros to ensure 2 digits
        else:
            # If this is the first ticket for the customer
            new_sequence = "01"

        # Generate new ticket number
        ticket_number = f"TK-{customer_id}{new_sequence}"
        return ticket_number

    except Exception as e:
        print(f"Error generating ticket number: {str(e)}")
        # Fallback to ensure we always return a ticket number
        return f"TK-{customer_id}00"

@app.route('/api/tickets', methods=['POST'])
@token_required
def create_ticket(current_user):
    try:
        data = request.form
        files = request.files.getlist('attachments')
        current_time = datetime.now(timezone.utc)

        # Get customer record
        customer = Customer.query.filter_by(user_id=current_user.user_id).first()
        if not customer:
            return jsonify({'error': 'Customer record not found'}), 404
        user = User.query.get(customer.user_id)
        if not user:
            return jsonify({'error': 'User record not found'}), 404
        # Create ticket
        new_ticket = Ticket(
            ticket_number=generate_ticket_number(customer.id),
            customer_id=customer.id,
            category_picklist=data.get('category'),
            status_picklist='Open',
            subject=data.get('subject'),
            description=data.get('description'),
            priority=data.get('priority').upper(),
            created_by=current_user.user_id,
            created_date=current_time,
            assigned_agent_id=None
        )

        db.session.add(new_ticket)
        db.session.flush()

        # Create initial response
        initial_response = TicketResponse(
            ticket_id=new_ticket.id,
            response_type='CUSTOMER_RESPONSE',
            response_text=data.get('description'),
            created_by=current_user.user_id,
            created_date=current_time
        )

        db.session.add(initial_response)
        db.session.flush()

        # Handle attachments
        saved_attachments = []
        if files:
            for file in files:
                if file.filename:
                    filename = secure_filename(f"{uuid.uuid4()}_{file.filename}")
                    file_directory = f"uploads/tickets/{new_ticket.id}"
                    os.makedirs(file_directory, exist_ok=True)
                    file_path = os.path.join(file_directory, filename)
                    file.save(file_path)

                    attachment = ResponseAttachment(
                        response_id=initial_response.id,
                        file_name=filename,
                        file_path=file_path,
                        mime_type=file.content_type,
                        created_by=current_user.user_id
                    )
                    db.session.add(attachment)
                    saved_attachments.append(attachment)
                    print(f"File saved at path: {file_path}")
                    print(f"Mime type: {file.content_type}")
                    print(f"Attachment created with ID: {attachment.id}")
        db.session.flush()

        # print(f"Saved attachments before notification: {[{
        #     'id': att.id,
        #     'file_name': att.file_name,
        #     'file_path': att.file_path,
        #     'exists': os.path.exists(att.file_path)
        # } for att in saved_attachments]}")
        # Create notification records
        notification = CustomerNotifications(
            customer_id=customer.id,
            subject=f"New Customer Ticket: {new_ticket.ticket_number}",
            message=data.get('description'),
            priority="HIGH",
            status="SENT",
            related_entity_type="TICKET",
            related_entity_id=new_ticket.id,
            created_by=current_user.user_id,
            created_date=current_time,
            last_modified_by=current_user.user_id,
            last_modified_date=current_time
        )
        db.session.add(notification)
        db.session.flush()

        # Create delivery record
        delivery = NotificationDeliveries(
            notification_id=notification.id,
            response_id=initial_response.id,
            channel='EMAIL',
            status='SENT',
            sent_date=current_time,
            created_by=current_user.user_id,
            created_date=current_time,
            last_modified_by=current_user.user_id,
            last_modified_date=current_time
        )
        db.session.add(delivery)

        # Find and notify support agents
        support_agents = User.query.filter(
            User.user_role.in_(['support_agent', 'administrator'])
        ).all()
        if support_agents:
            for agent in support_agents:
                app.logger.debug(
                f"Saved attachments before notification: {[att.file_name for att in saved_attachments]}")
                send_ticket_notification(
                    agent.email,
                    new_ticket.ticket_number,
                    new_ticket.subject,
                    new_ticket.description,
                    f"{current_user.first_name} {current_user.last_name}",
                    saved_attachments  # Pass the attachments to the notification function
                )

        db.session.commit()

        return jsonify({
            'message': 'Ticket created successfully',
            'ticket': {
                'id': new_ticket.id,
                'ticket_number': new_ticket.ticket_number,
                'attachments': [{
                    'id': att.id,
                    'file_name': att.file_name,
                } for att in saved_attachments]
            }
        }), 201

    except Exception as e:
        db.session.rollback()
        print(f"Error creating ticket: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/agent/tickets/create/<int:customer_id>', methods=['POST'])
@token_required
def create_agent_ticket(current_user, customer_id):
    try:
        if not current_user.is_support_agent and not current_user.is_admin:
            return jsonify({'error': 'Unauthorized access'}), 403

        customer = Customer.query.get(customer_id)
        if not customer:
            return jsonify({'error': 'Customer not found'}), 404

        user = User.query.get(customer.user_id)
        if not user:
            return jsonify({'error': 'Customer user not found'}), 404

        data = request.form
        files = request.files.getlist('attachments')
        current_time = datetime.now(timezone.utc)

        # Create ticket
        new_ticket = Ticket(
            ticket_number=generate_ticket_number(customer_id),
            customer_id=customer_id,
            category_picklist=data.get('category'),
            status_picklist='Open',
            subject=data.get('subject'),
            description=data.get('description'),
            priority=data.get('priority').upper(),
            created_by=current_user.user_id,
            created_date=current_time,
            assigned_agent_id=current_user.user_id
        )

        db.session.add(new_ticket)
        db.session.flush()

        # Create initial response
        initial_response = TicketResponse(
            ticket_id=new_ticket.id,
            response_type='AGENT_RESPONSE',
            response_text=data.get('description'),
            created_by=current_user.user_id,
            created_date=current_time
        )

        db.session.add(initial_response)
        db.session.flush()

        # Handle attachments
        saved_attachments = []
        if files:
            for file in files:
                if file.filename:
                    filename = secure_filename(f"{uuid.uuid4()}_{file.filename}")
                    file_directory = f"uploads/tickets/{new_ticket.id}"
                    os.makedirs(file_directory, exist_ok=True)
                    file_path = os.path.join(file_directory, filename)
                    file.save(file_path)

                    attachment = ResponseAttachment(
                        response_id=initial_response.id,
                        file_name=filename,
                        file_path=file_path,
                        mime_type=file.content_type,
                        created_by=current_user.user_id
                    )
                    db.session.add(attachment)
                    saved_attachments.append(attachment)

        db.session.flush()

        # Create notification records
        notification = CustomerNotifications(
            customer_id=customer_id,
            subject=f"New Support Ticket Created: {new_ticket.ticket_number}",
            message=data.get('description'),
            priority="HIGH",
            status="SENT",
            related_entity_type="TICKET",
            related_entity_id=new_ticket.id,
            created_by=current_user.user_id,
            created_date=current_time,
            last_modified_by=current_user.user_id,
            last_modified_date=current_time
        )
        db.session.add(notification)
        db.session.flush()

        # Create delivery record
        delivery = NotificationDeliveries(
            notification_id=notification.id,
            response_id=initial_response.id,
            channel='EMAIL',
            status='SENT',
            sent_date=current_time,
            created_by=current_user.user_id,
            created_date=current_time,
            last_modified_by=current_user.user_id,
            last_modified_date=current_time
        )
        db.session.add(delivery)

        # Send email notification to customer
        if user.email:
            send_ticket_notification(
                user.email,
                new_ticket.ticket_number,
                new_ticket.subject,
                new_ticket.description,
                current_user.first_name,
                saved_attachments,  # Pass the attachments to the notification function
                from_agent= True
            )

        db.session.commit()

        return jsonify({
            'message': 'Ticket created successfully',
            'ticket': {
                'id': new_ticket.id,
                'ticket_number': new_ticket.ticket_number,
                'subject': new_ticket.subject,
                'category': new_ticket.category_picklist,
                'status': new_ticket.status_picklist,
                'priority': new_ticket.priority,
                'created_date': new_ticket.created_date.isoformat(),
                'customer': {
                    'id': customer.id,
                    'name': f"{user.first_name} {user.last_name}",
                    'email': user.email
                },
                'description': new_ticket.description,
                'last_response_date': initial_response.created_date.isoformat(),
                'attachments': [{
                    'id': att.id,
                    'file_name': att.file_name,
                } for att in saved_attachments]
            }
        }), 201

    except Exception as e:
        db.session.rollback()
        print(f"Error creating agent ticket: {str(e)}")
        return jsonify({'error': str(e)}), 500

def send_ticket_notification(email, ticket_number, subject, description, sender_name, attachments=None,from_agent=False):
    """
    Send email notification for new ticket with attachments
    Uses email templates from config file based on whether ticket is created by agent or customer
    """
    try:
        templates = load_email_templates()
        # Get the appropriate template based on who created the ticket
        template_key = 'AGENT_CREATED' if from_agent else 'CUSTOMER_CREATED'
        email_template = templates['TICKET']['CREATION'][template_key]

        # Create attachment section if there are attachments
        attachment_section = ''
        if attachments:
            attachment_section = f'''
            <p><strong>Attachments:</strong></p>
            <ul style="margin: 0; padding-left: 20px;">
                {chr(10).join(f"<li>{att.file_name}</li>" for att in attachments)}
            </ul>
            '''

        # Format the HTML template with the provided values
        html_content = email_template['HTML'].format(
            sender_name=sender_name,
            ticket_number=ticket_number,
            subject=subject,
            description=description,
            attachment_section=attachment_section,
            portal_url=config['FLASK']['FRONTEND_LOGIN_URL']
        )

        # Create email message
        msg = Message(
            subject=email_template['SUBJECT'].format(ticket_number=ticket_number),
            recipients=[email],
            html=html_content,
            sender=app.config['MAIL_DEFAULT_SENDER']
        )

        # Attach files if any
        if attachments:
            for attachment in attachments:
                try:
                    with open(attachment.file_path, 'rb') as file:
                        msg.attach(
                            filename=attachment.file_name,
                            content_type=attachment.mime_type,
                            data=file.read()
                        )
                except Exception as file_error:
                    print(f"Error attaching file {attachment.file_name}: {str(file_error)}")
                    continue

        mail.send(msg)
        return True
    except Exception as e:
        print(f"Error sending email: {str(e)}")
        return False

@app.route('/api/customer/tickets', methods=['GET'])
@token_required
def get_tickets(current_user):
    try:
        # Calculate cutoff time for closed tickets
        visibility_hours = config['SETTINGS']['CLOSED_TICKET_VISIBILITY_HOURS']
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=visibility_hours)

        # Get customer record
        customer = Customer.query.filter_by(user_id=current_user.user_id).first()
        if not customer:
            return jsonify({'error': 'Customer record not found'}), 404

        # Base query with proper joins
        query = (db.session.query(Ticket)
                 .join(Customer, Ticket.customer_id == Customer.id)
                 .filter(Customer.user_id == current_user.user_id))

        # Add visibility filter for closed tickets
        query = query.filter(
            or_(
                Ticket.status_picklist != 'Closed',
                and_(
                    Ticket.status_picklist == 'Closed',
                    Ticket.last_modified_date >= cutoff_time
                )
            )
        )

        # Add proper order by
        tickets = query.order_by(Ticket.created_date.desc()).all()

        # Format response with all necessary fields
        tickets_data = [{
            'id': ticket.id,
            'ticket_number': ticket.ticket_number,
            'subject': ticket.subject,
            'category': ticket.category_picklist,
            'status': ticket.status_picklist.strip(),
            'priority': ticket.priority,
            'description': ticket.description,
            'created_date': ticket.created_date.isoformat() if ticket.created_date else None,
        } for ticket in tickets]

        return jsonify({
            'status': 'success',
            'tickets': tickets_data,
            'total': len(tickets_data)
        }), 200

    except Exception as e:
        print(f"Error fetching tickets: {str(e)}")
        return handle_ticket_error(e, "fetching tickets")
@app.route('/api/agent/tickets', methods=['GET'])
@token_required
def get_agent_tickets(current_user):
    """Get all tickets visible to the agent"""
    try:
        if not current_user.is_support_agent and not current_user.is_admin:
            return jsonify({'error': 'Unauthorized access'}), 403

        # Parse query parameters
        status = request.args.get('status')
        category = request.args.get('category')
        priority = request.args.get('priority')
        search = request.args.get('search')

        # Calculate the cutoff time for closed tickets
        visibility_hours = config['SETTINGS']['CLOSED_TICKET_VISIBILITY_HOURS']
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=visibility_hours)

        # Base query with joins for efficiency
        query = (db.session.query(Ticket, Customer, User)
                 .join(Customer, Ticket.customer_id == Customer.id)
                 .join(User, Customer.user_id == User.user_id))

        # Add visibility filter for closed tickets
        query = query.filter(
            or_(
                Ticket.status_picklist != 'Closed',
                and_(
                    Ticket.status_picklist == 'Closed',
                    Ticket.last_modified_date >= cutoff_time
                )
            )
        )

        # Apply other filters
        if status:
            query = query.filter(Ticket.status_picklist == status)
        if category:
            query = query.filter(Ticket.category_picklist == category)
        if priority:
            query = query.filter(Ticket.priority == priority)
        if search:
            search_term = f"%{search}%"
            query = query.filter(or_(
                Ticket.subject.ilike(search_term),
                Ticket.ticket_number.ilike(search_term),
                User.email.ilike(search_term)
            ))

        # Execute query
        results = query.order_by(desc(Ticket.created_date)).all()

        # Format response
        tickets_data = []
        for ticket, customer, user in results:
            ticket_data = {
                'id': ticket.id,
                'ticket_number': ticket.ticket_number,
                'subject': ticket.subject,
                'category': ticket.category_picklist,
                'status': ticket.status_picklist,
                'priority': ticket.priority,
                'created_date': ticket.created_date.isoformat(),
                'last_modified_date': ticket.last_modified_date.isoformat() if ticket.last_modified_date else None,
                'customer': {
                    'id': customer.id,
                    'name': f"{user.first_name} {user.last_name}",
                    'email': user.email
                },
                'description': ticket.description
            }

            # Get latest response date if exists
            latest_response = (TicketResponse.query
                              .filter_by(ticket_id=ticket.id)
                              .order_by(desc(TicketResponse.created_date))
                              .first())
            if latest_response:
                ticket_data['last_response_date'] = latest_response.created_date.isoformat()

            tickets_data.append(ticket_data)

        return jsonify({
            'tickets': tickets_data,
            'total': len(tickets_data)
        }), 200

    except Exception as e:
        print(f"Error fetching agent tickets: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/agent/customers', methods=['GET'])
@token_required
def get_agent_customers(current_user):
    """Get all customers for ticket creation"""
    try:
        if not current_user.is_support_agent and not current_user.is_admin:
            return jsonify({'error': 'Unauthorized access'}), 403

        # Get all customers with their details
        customers = (db.session.query(Customer, User)
                     .join(User, Customer.user_id == User.user_id)
                     .all())

        customers_data = [{
            'id': customer.id,
            'user_id': user.user_id,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'email': user.email
        } for customer, user in customers]

        return jsonify({
            'customers': customers_data
        }), 200

    except Exception as e:
        print(f"Error fetching customers: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/tickets/<int:ticket_id>/attachments/<int:attachment_id>', methods=['GET'])
@token_required
def get_attachment(current_user, ticket_id, attachment_id):
    try:
        attachment = ResponseAttachment.query.get(attachment_id)
        if not attachment:
            print(f"Attachment {attachment_id} not found")
            return jsonify({'error': 'Attachment not found'}), 404

        print(f"Attempting to send file: {attachment.file_path}")
        if not os.path.exists(attachment.file_path):
            print(f"File not found at path: {attachment.file_path}")
            return jsonify({'error': 'File not found'}), 404

        return send_file(
            attachment.file_path,
            mimetype=attachment.mime_type,
            as_attachment=True,
            download_name=attachment.file_name
        )
    except Exception as e:
        print(f"Error downloading attachment: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/agent/tickets/<int:ticket_id>', methods=['GET'])
@token_required
def get_ticket_details(current_user, ticket_id):
    """Get detailed ticket information including responses"""
    try:
        # Get ticket with customer and responses
        ticket = (db.session.query(Ticket, Customer, User)
                  .join(Customer, Ticket.customer_id == Customer.id)
                  .join(User, Customer.user_id == User.user_id)
                  .filter(Ticket.id == ticket_id)
                  .first())

        if not ticket:
            return jsonify({'error': 'Ticket not found'}), 404

        ticket_obj, customer, user = ticket

        # Get all responses with attachments
        responses = (TicketResponse.query
                     .filter_by(ticket_id=ticket_id)
                     .order_by(TicketResponse.created_date)
                     .all())

        responses_data = []
        for response in responses:
            # Get attachments for this response
            attachments = ResponseAttachment.query.filter_by(response_id=response.id).all()
            attachments_data = [{
                'id': att.id,
                'file_name': att.file_name,
                'mime_type': att.mime_type,
                'created_date': att.created_date.isoformat()
            } for att in attachments]

            responses_data.append({
                'id': response.id,
                'response_type': response.response_type,
                'response_text': response.response_text,
                'created_by': response.created_by,
                'created_date': response.created_date.isoformat(),
                'attachments': attachments_data
            })

        return jsonify({
            'ticket': {
                'id': ticket_obj.id,
                'ticket_number': ticket_obj.ticket_number,
                'subject': ticket_obj.subject,
                'description': ticket_obj.description,
                'status': ticket_obj.status_picklist,
                'priority': ticket_obj.priority,
                'created_date': ticket_obj.created_date.isoformat(),
                'customer': {
                    'id': customer.id,
                    'name': f"{user.first_name} {user.last_name}",
                    'email': user.email
                },
                'responses': responses_data
            }
        }), 200

    except Exception as e:
        print(f"Error fetching ticket details: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/tickets/<int:ticket_id>/responses', methods=['POST'])
@token_required
def add_ticket_response(current_user, ticket_id):
    try:
        # Get customer record
        customer = Customer.query.filter_by(user_id=current_user.user_id).first()
        if not customer:
            return jsonify({'error': 'Customer record not found'}), 404

        # Get ticket and verify ownership
        ticket = Ticket.query.filter_by(id=ticket_id, customer_id=customer.id).first()
        if not ticket:
            return jsonify({'error': 'Ticket not found or unauthorized access'}), 404

        response_text = request.form.get('response_text', '').strip()
        files = request.files.getlist('attachments')
        current_time = datetime.now(timezone.utc)

        if not response_text and not files:
            return jsonify({'error': 'Please provide either a message or attachments'}), 400

        # Create new response
        new_response = TicketResponse(
            ticket_id=ticket_id,
            response_type='CUSTOMER_RESPONSE',
            response_text=response_text or "Attachments provided",
            created_by=current_user.user_id,
            created_date=current_time,
            last_modified_by=current_user.user_id,
            last_modified_date=current_time
        )

        db.session.add(new_response)
        db.session.flush()

        # Handle file attachments
        saved_attachments = []
        if files:
            base_upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'tickets', str(ticket_id), 'responses', str(new_response.id))
            os.makedirs(base_upload_dir, exist_ok=True)

            for file in files:
                if file and file.filename:
                    try:
                        original_filename = secure_filename(file.filename)
                        unique_filename = f"{uuid.uuid4()}_{original_filename}"
                        file_path = os.path.join(base_upload_dir, unique_filename)
                        file.save(file_path)

                        attachment = ResponseAttachment(
                            response_id=new_response.id,
                            file_name=original_filename,
                            file_path=file_path,
                            mime_type=file.content_type or 'application/octet-stream',
                            created_by=current_user.user_id
                        )
                        db.session.add(attachment)
                        saved_attachments.append(attachment)
                    except Exception as e:
                        print(f"Error saving attachment {file.filename}: {str(e)}")

        # Fetch all support agents
        support_agents = User.query.filter(
            User.user_role.in_(['support_agent']),
            User.status == 'active'
        ).all()

        if support_agents:
            # Create notification record once
            notification = CustomerNotifications(
                customer_id=customer.id,
                subject=f"New Response to Ticket #{ticket.ticket_number}",
                message=f"Customer {current_user.first_name} {current_user.last_name} has responded to ticket #{ticket.ticket_number}",
                priority="HIGH",
                status="PENDING",
                created_by=current_user.user_id,
                created_date=current_time,
                last_modified_by=current_user.user_id,
                last_modified_date=current_time
            )
            db.session.add(notification)
            db.session.flush()

            # Prepare email content
            templates = load_email_templates()
            template = templates['TICKET']['response']['to_agent']
            attachment_list = ""
            if saved_attachments:
                attachment_list = f"""
                            <ul style="margin: 0; padding-left: 20px;">
                                {"".join(f"<li>{att.file_name}</li>" for att in saved_attachments)}
                            </ul>
                            """
            formatted_html = template['HTML'].format(
                customer_name=f"{current_user.first_name} {current_user.last_name}",
                ticket_number=ticket.ticket_number,
                subject=ticket.subject,
                response_text=response_text,
                attachment_list=attachment_list,
                portal_url=config['FLASK']['FRONTEND_LOGIN_URL']
            )

            # Send email to all support agents
            for agent in support_agents:
                try:
                    if agent.email:

                        msg = Message(
                            subject=template['SUBJECT'].format(ticket_number=ticket.ticket_number),
                            recipients=[agent.email],
                            html=formatted_html,
                            sender=app.config['MAIL_DEFAULT_SENDER']
                        )

                        # Attach files to email
                        for attachment in saved_attachments:
                            with open(attachment.file_path, 'rb') as f:
                                msg.attach(
                                    filename=attachment.file_name,
                                    content_type=attachment.mime_type,
                                    data=f.read()
                                )

                        mail.send(msg)

                        # Record delivery for each successful email
                        delivery = NotificationDeliveries(
                            notification_id=notification.id,
                            response_id=new_response.id,
                            channel='EMAIL',
                            status='SENT',
                            sent_date=current_time,
                            created_by=current_user.user_id,
                            created_date=current_time,
                            last_modified_by=current_user.user_id,
                            last_modified_date=current_time
                        )
                        db.session.add(delivery)

                except Exception as e:
                    print(f"Error sending notification to agent {agent.email}: {str(e)}")
                    # Continue with other agents even if one fails

        # Update ticket last modified info
        ticket.last_modified_by = current_user.user_id
        ticket.last_modified_date = current_time

        db.session.commit()

        return jsonify({
            'status': 'success',
            'message': 'Response added successfully',
            'response': {
                'id': new_response.id,
                'text': response_text,
                'created_date': current_time.isoformat(),
                'attachments': [{
                    'id': att.id,
                    'name': att.file_name,
                    'mime_type': att.mime_type
                } for att in saved_attachments]
            }
        }), 201

    except Exception as e:
        db.session.rollback()
        print(f"Error adding response: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/agent/tickets/<int:ticket_id>/responses', methods=['POST'])
@token_required
def add_agent_response(current_user, ticket_id):
    try:
        if not current_user.is_support_agent and not current_user.is_admin:
            return jsonify({'error': 'Unauthorized access'}), 403

        ticket = Ticket.query.get(ticket_id)
        if not ticket:
            return jsonify({'error': 'Ticket not found'}), 404

        # Get response text from form data
        response_text = request.form.get('response_text', '').strip()
        files = request.files.getlist('attachments')

        # Check if there's neither message nor attachments
        if not response_text and not files:
            return jsonify({'error': 'Please provide either a message or attachments'}), 400

        current_time = datetime.now(timezone.utc)

        # If no text message but has attachments, create a default message
        if not response_text and files:
            response_text = "Attachments provided"

        # Create new response
        new_response = TicketResponse(
            ticket_id=ticket_id,
            response_type='AGENT_RESPONSE',
            response_text=response_text,
            created_by=current_user.user_id,
            created_date=current_time,
            last_modified_by=current_user.user_id,
            last_modified_date=current_time
        )

        db.session.add(new_response)
        db.session.flush()

        # Handle file attachments
        saved_attachments = []
        for file in files:
            if file:
                try:
                    filename = secure_filename(f"{uuid.uuid4()}_{file.filename}")
                    file_directory = f"uploads/tickets/{ticket_id}/responses/{new_response.id}"
                    os.makedirs(file_directory, exist_ok=True)
                    file_path = os.path.join(file_directory, filename)
                    file.save(file_path)

                    attachment = ResponseAttachment(
                        response_id=new_response.id,
                        file_name=filename,
                        file_path=file_path,
                        mime_type=file.content_type,
                        created_by=current_user.user_id
                    )
                    db.session.add(attachment)
                    saved_attachments.append(attachment)
                except Exception as e:
                    print(f"Error saving attachment {file.filename}: {str(e)}")
                    continue

        db.session.flush()

        # Send notification to customer
        customer = Customer.query.get(ticket.customer_id)
        user = User.query.get(customer.user_id)
        templates = load_email_templates()
        template = templates['TICKET']['response']['to_customer']
        if user and user.email:
            try:
                # Create attachment list for email
                attachment_list = ""
                if saved_attachments:
                    attachment_list = f"""
                                <ul style="margin: 0; padding-left: 20px;">
                                    {"".join(f"<li>{att.file_name}</li>" for att in saved_attachments)}
                                </ul>
                                """

                # Use template from config
                formatted_html = template['HTML'].format(
                    agent_name=f"{current_user.first_name} {current_user.last_name}",
                    ticket_number=ticket.ticket_number,
                    subject=ticket.subject,
                    response_text=response_text,
                    attachment_list=attachment_list,
                    portal_url=config['FLASK']['FRONTEND_LOGIN_URL']
                )

                # Create and send email with attachments
                msg = Message(
                    subject=template['SUBJECT'].format(ticket_number=ticket.ticket_number),
                    recipients=[user.email],
                    html=formatted_html,
                    sender=app.config['MAIL_DEFAULT_SENDER']
                )

                # Attach files to email
                for attachment in saved_attachments:
                    try:
                        with open(attachment.file_path, 'rb') as file:
                            msg.attach(
                                filename=attachment.file_name,
                                content_type=attachment.mime_type,
                                data=file.read()
                            )
                    except Exception as e:
                        print(f"Error attaching file {attachment.file_name} to email: {str(e)}")
                        continue

                mail.send(msg)

                # Create notification record
                notification = CustomerNotifications(
                    customer_id=customer.id,
                    subject=f"New Response to Ticket #{ticket.ticket_number}",
                    message=formatted_html,
                    priority="HIGH",
                    status="PENDING",
                    related_entity_type="TICKET_RESPONSE",
                    related_entity_id=new_response.id,
                    created_by=current_user.user_id,
                    created_date=current_time,
                    last_modified_by=current_user.user_id,
                    last_modified_date=current_time
                )
                db.session.add(notification)
                db.session.flush()

                # Create notification delivery record
                delivery = NotificationDeliveries(
                    notification_id=notification.id,
                    response_id=new_response.id,
                    channel='EMAIL',
                    status='SENT',
                    sent_date=current_time,
                    created_by=current_user.user_id,
                    created_date=current_time,
                    last_modified_by=current_user.user_id,
                    last_modified_date=current_time
                )
                db.session.add(delivery)

            except Exception as e:
                print(f"Error sending notification email: {str(e)}")

        # Update ticket last modified info
        ticket.last_modified_by = current_user.user_id
        ticket.last_modified_date = current_time

        db.session.commit()

        return jsonify({
            'status': 'success',
            'message': 'Response added successfully',
            'response': {
                'id': new_response.id,
                'response_text': response_text,
                'response_type': 'AGENT_RESPONSE',
                'created_by': current_user.user_id,
                'created_date': current_time.isoformat(),
                'attachments': [{
                    'file_name': att.file_name,
                    'mime_type': att.mime_type
                } for att in saved_attachments],
                'sender_name': f"{current_user.first_name} {current_user.last_name}"
            }
        }), 201

    except Exception as e:
        db.session.rollback()
        print(f"Error adding response: {str(e)}")
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': f'Failed to add response: {str(e)}'
        }), 500

@app.route('/api/tickets/<int:ticket_id>/latest-response', methods=['GET'])
@token_required
def get_latest_response(current_user, ticket_id):
    try:
        # Get the latest response for the ticket
        latest_response = TicketResponse.query.filter_by(ticket_id=ticket_id) \
            .order_by(TicketResponse.created_date.desc()) \
            .first()

        if not latest_response:
            return jsonify({'response_id': None}), 200

        return jsonify({'response_id': latest_response.id}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/tickets/<int:ticket_id>/attachments/<int:attachment_id>', methods=['GET'])
@token_required
def get_ticket_attachment(current_user, ticket_id, attachment_id):
    try:
        # Get the attachment
        attachment = ResponseAttachment.query.get(attachment_id)
        if not attachment:
            return jsonify({'error': 'Attachment not found'}), 404

        # Get the associated ticket response
        response = TicketResponse.query.get(attachment.response_id)
        if not response or response.ticket_id != ticket_id:
            return jsonify({'error': 'Invalid attachment'}), 404

        # Get the ticket
        ticket = Ticket.query.get(ticket_id)
        if not ticket:
            return jsonify({'error': 'Ticket not found'}), 404

        # Check authorization
        if current_user.is_support_agent or current_user.is_admin:
            # Agents and admins can access all attachments
            authorized = True
        else:
            # For customers, check if they own the ticket
            customer = Customer.query.filter_by(user_id=current_user.user_id).first()
            authorized = customer and ticket.customer_id == customer.id

        if not authorized:
            return jsonify({'error': 'Unauthorized access'}), 403

        # Send the file
        return send_file(
            attachment.file_path,
            mimetype=attachment.mime_type,
            as_attachment=True,
            download_name=attachment.file_name
        )

    except Exception as e:
        print(f"Error serving attachment: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/agent/tickets/<int:ticket_id>/attachments/<int:attachment_id>', methods=['GET'])
@token_required
def download_attachment(current_user, ticket_id, attachment_id):
    try:
        attachment = ResponseAttachment.query.get(attachment_id)
        if not attachment:
            return jsonify({'error': 'Attachment not found'}), 404

        # Verify the attachment belongs to the ticket
        if attachment.response.ticket_id != ticket_id:
            return jsonify({'error': 'Invalid attachment'}), 403

        return send_file(
            attachment.file_path,
            mimetype=attachment.mime_type,
            as_attachment=True,
            download_name=attachment.file_name
        )

    except Exception as e:
        print(f"Error downloading attachment: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/agent/tickets/<int:ticket_id>/status', methods=['PUT'])
@token_required
def update_ticket_status(current_user, ticket_id):
    try:
        # if not current_user.is_support_agent and not current_user.is_admin:
        #     return jsonify({'error': 'Unauthorized access'}), 403

        ticket = Ticket.query.get(ticket_id)
        if not ticket:
            return jsonify({'error': 'Ticket not found'}), 404

        data = request.get_json()
        new_status = data.get('status')
        reason = data.get('reason')
        sender_user_id = data.get('sender_user_id')
        response_id = data.get('response_id')

        if not new_status or new_status not in ['Open', 'Inprogress', 'Closed']:
            return jsonify({'error': 'Invalid status'}), 400

        if not reason:
            return jsonify({'error': 'Reason is required'}), 400

        # Store old status for notification
        old_status = ticket.status_picklist
        current_time = datetime.now(timezone.utc)
        is_customer = bool(Customer.query.filter_by(user_id=current_user.user_id).first())

        # Set response type based on user role
        response_type = 'CUSTOMER_RESPONSE' if is_customer else 'AGENT_RESPONSE'

        # Update ticket status
        ticket.status_picklist = new_status
        ticket.last_modified_by = current_user.user_id
        ticket.last_modified_date = current_time

        # Add system note about status change
        system_note = TicketResponse(
            ticket_id=ticket_id,
            response_type=response_type,
            response_text=f"Ticket status changed from {old_status} to {new_status} by {current_user.first_name} {current_user.last_name}. Reason: {reason}",
            created_by=current_user.user_id,
            created_date=current_time
        )
        db.session.add(system_note)
        db.session.flush()

        # Get customer details for notification
        customer = Customer.query.get(ticket.customer_id)
        user = User.query.get(customer.user_id) if customer else None

        # Load templates from config
        templates = load_email_templates()
        template_key = 'to_customer' if is_customer else 'to_agent'
        template = templates['TICKET']['status'][template_key]

        # Send email notification
        if user and user.email:
            # Create notification record
            notification = CustomerNotifications(
                customer_id=customer.id,
                subject=f"Ticket {ticket.ticket_number} Status Updated",
                message=f"Ticket status changed to {new_status}. Reason: {reason}",
                priority="HIGH",
                status="SENT",
                created_by=current_user.user_id,
                created_date=current_time,
                last_modified_by=current_user.user_id,
                last_modified_date=current_time
            )
            db.session.add(notification)
            db.session.flush()

            # Create notification delivery record
            delivery = NotificationDeliveries(
                notification_id=notification.id,
                response_id=system_note.id,
                channel='EMAIL',
                status='SENT',
                sent_date=current_time,
                created_by=sender_user_id,
                created_date=current_time,
                last_modified_by=sender_user_id,
                last_modified_date=current_time
            )
            db.session.add(delivery)

            # Prepare template parameters
            template_params = {
                'ticket_number': ticket.ticket_number,
                'old_status': old_status,
                'new_status': new_status,
                'updater_name': f"{current_user.first_name} {current_user.last_name}",
                'reason': reason,
                'portal_url': config['FLASK']['FRONTEND_LOGIN_URL'],
                'customer_name': f"{user.first_name} {user.last_name}",
                'subject': ticket.subject,
                'description': ticket.description
            }

            # Format email content using template
            formatted_html = template['HTML'].format(**template_params)
            formatted_subject = template['SUBJECT'].format(**template_params)

            msg = Message(
                subject=formatted_subject,
                recipients=[user.email],
                html=formatted_html,
                sender=app.config['MAIL_DEFAULT_SENDER']
            )
            mail.send(msg)

        db.session.commit()

        return jsonify({
            'message': 'Ticket status updated successfully',
            'ticket': {
                'id': ticket.id,
                'ticket_number': ticket.ticket_number,
                'status': new_status,
                'old_status': old_status,
                'last_modified_date': ticket.last_modified_date.isoformat(),
                'modified_by': f"{current_user.first_name} {current_user.last_name}"
            }
        }), 200

    except Exception as e:
        db.session.rollback()
        print(f"Error updating ticket status: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/tickets/statistics', methods=['GET'])
@token_required
def get_ticket_statistics(current_user):
    """Get ticket statistics and metrics"""
    try:
        # Base query
        query = Ticket.query

        # Calculate cutoff time for closed tickets
        visibility_hours = config['SETTINGS']['CLOSED_TICKET_VISIBILITY_HOURS']
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=visibility_hours)

        # Apply role-based filters
        if current_user.user_role == 'customer':
            customer = Customer.query.filter_by(user_id=current_user.user_id).first()
            if not customer:
                return jsonify({'error': 'Customer record not found'}), 404
            query = query.filter_by(customer_id=customer.id)
        elif current_user.user_role == 'support_agent':
            query = query.filter_by(assigned_agent_id=current_user.user_id)

        # Calculate statistics
        # Open tickets
        open_tickets = query.filter_by(status_picklist='Open').count()

        # In-progress tickets
        in_progress = query.filter_by(status_picklist='Inprogress').count()

        # Visible closed tickets
        closed_tickets = query.filter(
            and_(
                Ticket.status_picklist == 'Closed',
                Ticket.last_modified_date >= cutoff_time
            )
        ).count()

        # Calculate total visible tickets
        total_tickets = open_tickets + in_progress + closed_tickets

        # Calculate response rate (tickets with at least one response / total tickets)
        tickets_with_responses = (query.join(TicketResponse)
                                  .filter(or_(
            Ticket.status_picklist != 'Closed',
            and_(
                Ticket.status_picklist == 'Closed',
                Ticket.last_modified_date >= cutoff_time
            )
        ))
                                  .distinct()
                                  .count())

        response_rate = (tickets_with_responses / total_tickets * 100) if total_tickets > 0 else 0

        return jsonify({
            'total_tickets': total_tickets,
            'open_tickets': open_tickets,
            'in_progress': in_progress,
            'closed': closed_tickets,
            'response_rate': round(response_rate, 2)
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/agent/tickets/statistics', methods=['GET'])
@token_required
def get_agent_ticket_statistics(current_user):
    """Get statistics for all tickets"""
    try:
        if not current_user.is_support_agent and not current_user.is_admin:
            return jsonify({'error': 'Unauthorized access'}), 403

        # Calculate cutoff time for closed tickets
        visibility_hours = config['SETTINGS']['CLOSED_TICKET_VISIBILITY_HOURS']
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=visibility_hours)

        # Get counts for different statuses
        open_count = Ticket.query.filter_by(status_picklist='Open').count()
        in_progress_count = Ticket.query.filter_by(status_picklist='Inprogress').count()

        # Apply visibility window to closed tickets
        resolved_count = Ticket.query.filter(
            and_(
                Ticket.status_picklist == 'Closed',
                Ticket.last_modified_date >= cutoff_time
            )
        ).count()

        # Calculate total tickets (now only including visible closed tickets)
        total_tickets = open_count + in_progress_count + resolved_count

        # Calculate response rate based on in-progress and visible resolved tickets
        responded_tickets = in_progress_count + resolved_count
        response_rate = (responded_tickets / total_tickets * 100) if total_tickets > 0 else 0

        # Get tickets created today
        today = datetime.now(timezone.utc).date()
        tickets_today = (Ticket.query
                         .filter(func.date(Ticket.created_date) == today)
                         .count())

        return jsonify({
            'total_tickets': total_tickets,
            'open_tickets': open_count,
            'in_progress': in_progress_count,
            'resolved': resolved_count,
            'response_rate': round(response_rate, 2),
            'tickets_today': tickets_today
        }), 200

    except Exception as e:
        print(f"Error fetching ticket statistics: {str(e)}")
        return jsonify({'error': str(e)}), 500

def handle_ticket_error(e, operation):
    """Centralized error handling for ticket operations"""
    error_msg = f"Error during {operation}: {str(e)}"
    traceback.print_exc()
    print(error_msg)

    if isinstance(e, SQLAlchemyError):
        db.session.rollback()
        return jsonify({
            'status': 'error',
            'message': 'Database error occurred',
            'details': str(e)
        }), 500

    return jsonify({
        'status': 'error',
        'message': error_msg
    }), 500




@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    print('Client connected')
@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    print('Client disconnected')
@socketio.on('join')
def on_join(data):
    """Join a specific ticket room"""
    room = data.get('ticket_id')
    if room:
        join_room(room)
        emit('status', {'msg': f'Joined room: {room}'})
@socketio.on('leave')
def on_leave(data):
    """Leave a specific ticket room"""
    room = data.get('ticket_id')
    if room:
        leave_room(room)
        emit('status', {'msg': f'Left room: {room}'})
def notify_ticket_update(ticket_id, update_type, data):
    """Notify clients about ticket updates"""
    socketio.emit('ticket_update', {
        'ticket_id': ticket_id,
        'type': update_type,
        'data': data
    }, room=str(ticket_id))


# admin dashboard agent creation customer and agent password
@app.route('/get_users', methods=['GET'])
@token_required
def get_users(current_user):
    try:
        # Check if user is admin
        if not current_user.is_admin:
            return jsonify({"error": "Unauthorized access"}), 403

        # Query to get all users who are not customers
        users = User.query.filter(
            User.user_role.in_(['support_agent', 'administrator'])
        ).all()

        users_data = [{
            'id': user.user_id,
            'email': user.email,
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'user_role': user.user_role,
            'status': user.status,
            'last_login': user.last_login.isoformat() if user.last_login else None,
            'created_date': user.created_date.isoformat() if user.created_date else None
        } for user in users]

        return jsonify({
            'status': 'success',
            'users': users_data
        }), 200

    except Exception as e:
        print(f"Error fetching users: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"An error occurred while fetching users: {str(e)}"
        }), 500
@app.route('/get_customers', methods=['GET'])
@token_required
def get_customers(current_user):
    try:
        if not current_user.is_admin:
            return jsonify({"error": "Unauthorized access"}), 403

        # Query customers with their user information
        customers = (db.session.query(Customer, User)
                     .join(User, Customer.user_id == User.user_id)
                     .filter(User.user_role == 'customer')
                     .all())

        customers_data = []
        for customer, user in customers:
            customer_info = {
                'id': customer.id,
                'user_id': user.user_id,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'status': user.status,
                'created_date': customer.created_date.isoformat() if customer.created_date else None,
                'marital_status': customer.marital_status,
                'address': customer.address,
                'city': customer.city,
                'occupation': customer.occupation,
                'tax_year': datetime.now().year - 1,  # Current tax year

                # Additional customer-specific fields
                'documents_count': Document.query.filter_by(customer_id=customer.id).count(),
                'last_submission': Document.query.filter_by(
                    customer_id=customer.id,
                    status='SUBMITTED'
                ).order_by(Document.upload_date.desc()).first().upload_date.isoformat() if Document.query.filter_by(
                    customer_id=customer.id,
                    status='SUBMITTED'
                ).first() else None
            }
            customers_data.append(customer_info)

        return jsonify({
            'status': 'success',
            'customers': customers_data
        }), 200

    except Exception as e:
        print(f"Error fetching customers: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"An error occurred while fetching customers: {str(e)}"
        }), 500
def generate_password(length=12):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(secrets.choice(alphabet) for i in range(length))
@app.route('/user/toggle-status/<int:user_id>', methods=['POST'])
@token_required
def toggle_user_status(current_user, user_id):
    try:
        if not current_user.is_admin:
            return jsonify({"error": "Unauthorized"}), 403

        user = User.query.get(user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        # Toggle status between 'active' and 'inactive'
        user.status = 'inactive' if user.status == 'active' else 'active'

        # Create notification based on user role
        notification_subject = f"Account {user.status.capitalize()}"
        notification_message = f"Your account has been {user.status}"

        if user.user_role == 'customer':
            customer = Customer.query.filter_by(user_id=user.user_id).first()
            customer_id = customer.id if customer else None
        else:
            customer_id = None

        notification = CustomerNotifications(
            customer_id=customer_id,
            subject=notification_subject,
            message=notification_message,
            priority="HIGH",
            status="PENDING",
            created_by=current_user.user_id,
            created_date=datetime.now(timezone.utc),
            last_modified_by=current_user.user_id,
            last_modified_date=datetime.now(timezone.utc)
        )

        db.session.add(notification)
        db.session.commit()

        return jsonify({
            "status": "success",
            "message": f"User status updated to {user.status}",
            "new_status": user.status
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500
@app.route('/user/generate-password/<int:user_id>', methods=['POST'])
@token_required
def generate_user_password(current_user, user_id):
    try:
        if not current_user.is_admin:
            return jsonify({"error": "Unauthorized"}), 403

        user = User.query.get(user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        # Generate new password
        new_password = generate_password()
        user.password_hash = generate_password_hash(new_password)

        # Get customer_id if user is a customer
        if user.user_role == 'customer':
            customer = Customer.query.filter_by(user_id=user.user_id).first()
            customer_id = customer.id if customer else None
        else:
            customer_id = None

        # Create notification record
        notification = CustomerNotifications(
            customer_id=customer_id,
            subject="Password Reset Notification",
            message=f"Password reset for user {user.email}",
            priority="HIGH",
            status="PENDING",
            created_by=current_user.user_id,
            created_date=datetime.now(timezone.utc),
            last_modified_by=current_user.user_id,
            last_modified_date=datetime.now(timezone.utc)
        )

        db.session.add(notification)
        db.session.flush()

        # Send email with new password
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6;">
            <h2>Your Password Has Been Reset</h2>
            <p>Dear {user.first_name} {user.last_name},</p>
            <p>Your password has been reset. Here are your new credentials:</p>
            <div style="background-color: #f5f5f5; padding: 15px; border-radius: 5px;">
                <p><strong>Email:</strong> {user.email}</p>
                <p><strong>Password:</strong> {new_password}</p>
            </div>
            <p>Please change your password after logging in.</p>
            <p>Best regards,<br>Support Team</p>
        </body>
        </html>
        """

        try:
            msg = Message(
                'Password Reset Notification',
                recipients=[user.email],
                html=html_content,
                sender=app.config['MAIL_DEFAULT_SENDER']
            )
            mail.send(msg)
            email_status = 'SENT'
        except Exception as e:
            print(f"Failed to send email: {str(e)}")
            email_status = 'FAILED'

        # Record delivery
        delivery = NotificationDeliveries(
            notification_id=notification.id,
            channel='EMAIL',
            status=email_status,
            sent_date=datetime.now(timezone.utc) if email_status == 'SENT' else None,
            error_message=str(e) if email_status == 'FAILED' else None,
            created_by=current_user.user_id,
            created_date=datetime.now(timezone.utc),
            last_modified_by=current_user.user_id,
            last_modified_date=datetime.now(timezone.utc)
        )

        db.session.add(delivery)
        db.session.commit()

        return jsonify({
            "status": "success",
            "message": "Password generated and sent to user's email",
            "email_status": email_status
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500
@app.route('/create_support_agent', methods=['POST'])
@token_required
def create_support_agent(current_user):
    if not current_user.is_admin:
        return jsonify({"message": "Admin privilege required"}), 403

    data = request.json
    email = data.get('email')
    first_name = data.get('first_name')
    last_name = data.get('last_name')

    if not email or not first_name or not last_name:
        return jsonify({"message": "Email, first name, and last name are required"}), 400

    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        return jsonify({"message": "User with this email already exists"}), 409

    declaration_token = str(uuid.uuid4())
    username = email.split('@')[0]

    new_user = User(
        email=email,
        username=username,
        user_role='support_agent',
        status='pending',
        declaration_token=declaration_token,
        is_declared=False,
        password_hash='',
        first_name=first_name,
        last_name=last_name,
        tenant_id=1
    )

    db.session.add(new_user)

    try:
        db.session.commit()

        # Generate the frontend URL for the declaration page
        setup_link = f"{app.config['FRONTEND_URL']}/agent-setup/{declaration_token}?email={email}"
        print("\n====== SUPPORT AGENT SETUP LINK ======")
        print(f"Email: {email}")
        print(f"Name: {first_name} {last_name}")
        print(f"Setup Link: {setup_link}")
        print("=====================================\n")
        # Send email with the declaration link
        email_sent = send_declaration_email(email, setup_link)

        response_message = "New support agent created successfully"
        if not email_sent:
            response_message += " (Warning: Email delivery failed)"

        return jsonify({
            "message": response_message,
            "setup_link": setup_link,
            "user_email": email
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Error creating support agent: {str(e)}"}), 500
@app.route('/complete_agent_setup', methods=['POST'])
def complete_agent_setup():
    data = request.json
    token = data.get('token')
    password = data.get('password')
    email = data.get('email')

    if not token or not password or not email:
        return jsonify({"message": "Token, password and email are required"}), 400

    user = User.query.filter_by(declaration_token=token, email=email).first()

    if not user:
        return jsonify({"message": "Invalid token or email"}), 404

    if user.user_role != 'support_agent':
        return jsonify({"message": "Invalid user type"}), 403

    if user.is_declared:
        return jsonify({"message": "Setup already completed"}), 409

    try:
        # Update user status and password
        user.password_hash = generate_password_hash(password)
        user.status = 'active'
        user.is_declared = True
        user.declaration_token = None  # Invalidate the token after use

        db.session.commit()

        return jsonify({
            "message": "Agent setup completed successfully",
            "email": user.email
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Error completing setup: {str(e)}"}), 500


# agent dashboard document types and download all documents
@app.route('/api/sortdocument-types', methods=['GET'])
@token_required
def get_sorted_document_types(current_user):
    try:
        # Get all document types that have associated documents
        document_types = (db.session.query(DocumentType)
                         .join(Document, DocumentType.document_type_id == Document.document_type_id)
                         .filter(Document.document_type_id != 48)  # Exclude specific type if needed
                         .distinct()
                         .order_by(DocumentType.type_name)
                         .all())

        types_data = [{
            'id': doc_type.document_type_id,
            'name': doc_type.type_name,
            'category': doc_type.category_name
        } for doc_type in document_types]

        return jsonify({
            'status': 'success',
            'document_types': types_data
        }), 200

    except Exception as e:
        print(f"Error fetching document types: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/agent/customer/<int:customer_id>/download-all-documents', methods=['GET'])
@token_required
def download_all_customer_documents(current_user, customer_id):
    try:
        if not current_user.is_support_agent and not current_user.is_admin:
            return jsonify({'error': 'Unauthorized access'}), 403

        customer = Customer.query.get(customer_id)
        if not customer:
            return jsonify({'error': 'Customer not found'}), 404

        documents = Document.query.filter(
            Document.customer_id == customer_id,
            Document.status == 'SUBMITTED',
            Document.is_deleted == False
        ).all()

        if not documents:
            return jsonify('No documents available for download'), 404

        temp_dir = tempfile.mkdtemp()
        output_path = os.path.join(temp_dir, 'merged.pdf')

        try:
            merger = PyPDF2.PdfMerger()
            image_files = []

            for doc in documents:
                if not os.path.exists(doc.file_path):
                    continue

                try:
                    # Check file type
                    mime_type = doc.mime_type.lower()

                    if mime_type.startswith('image/'):
                        # Convert image to PDF
                        image = Image.open(doc.file_path)
                        if image.mode == 'RGBA':
                            image = image.convert('RGB')

                        pdf_path = os.path.join(temp_dir, f'{uuid.uuid4()}.pdf')
                        image.save(pdf_path, 'PDF', resolution=100.0)
                        image_files.append(pdf_path)

                        with open(pdf_path, 'rb') as pdf:
                            merger.append(pdf)

                    elif mime_type == 'application/pdf':
                        with open(doc.file_path, 'rb') as file:
                            if file.read(4).startswith(b'%PDF'):
                                file.seek(0)
                                merger.append(file)

                except Exception as e:
                    print(f"Error processing file {doc.file_path}: {str(e)}")
                    continue

            if len(merger.pages) == 0:
                raise ValueError("No valid documents found")

            merger.write(output_path)
            merger.close()

            user = User.query.get(customer.user_id)
            filename = f"{user.first_name}_{user.last_name}_AllDocuments_{datetime.now().strftime('%Y%m%d')}.pdf"

            return send_file(
                output_path,
                mimetype='application/pdf',
                as_attachment=True,
                download_name=filename
            )

        finally:
            # Cleanup temporary files
            for img_file in image_files:
                try:
                    os.remove(img_file)
                except:
                    pass
            try:
                shutil.rmtree(temp_dir)
            except:
                pass

    except Exception as e:
        print(f"Error downloading customer documents: {str(e)}")
        return jsonify({
            'error': 'Failed to download documents',
            'details': str(e)
        }), 500

# reset password and magic link
@app.route('/validate-credentials', methods=['POST'])
def validate_credentials():
    try:
        data = request.get_json()
        email = data.get('email')
        password = data.get('oldPassword')

        if not email or not password:
            return jsonify({"message": "Email and password are required"}), 400

        # Find user by email
        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({"message": "Invalid email or password"}), 401

        # Verify password
        if not check_password_hash(user.password_hash, password):
            return jsonify({"message": "Invalid email or password"}), 401

        return jsonify({
            "message": "Credentials validated successfully",
            "user_id": user.user_id
        }), 200

    except Exception as e:
        print(f"Error validating credentials: {str(e)}")
        return jsonify({"message": "An error occurred during validation"}), 500
@app.route('/reset-password', methods=['POST'])
def reset_password():
    try:
        data = request.get_json()
        email = data.get('email')
        old_password = data.get('oldPassword')
        new_password = data.get('newPassword')

        if not all([email, old_password, new_password]):
            return jsonify({"message": "All fields are required"}), 400

        # Find user
        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({"message": "User not found"}), 404

        # Get customer record if user is a customer
        customer = None
        if user.user_role == 'customer':
            customer = Customer.query.filter_by(user_id=user.user_id).first()

        # Verify old password
        if not check_password_hash(user.password_hash, old_password):
            return jsonify({"message": "Invalid current password"}), 401

        # Update password
        user.password_hash = generate_password_hash(new_password)

        # Send confirmation email
        try:
            reset_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            msg = Message(
                subject=config['EMAIL_TEMPLATES']['PASSWORD_RESET']['CONFIRMATION']['SUBJECT'],
                recipients=[user.email],
                html=config['EMAIL_TEMPLATES']['PASSWORD_RESET']['CONFIRMATION']['HTML'].format(
                    user_email=user.email,
                    reset_time=reset_time,
                    portal_url=config['FLASK']['FRONTEND_URL']
                )
            )
            mail.send(msg)

            # Create notification record
            notification = CustomerNotifications(
                customer_id=customer.id if customer else None,
                subject="Password Reset Confirmation",
                message=f"Password reset completed for {user.email}",
                priority="HIGH",
                status="SENT",
                related_entity_type="PASSWORD_RESET",
                related_entity_id=user.user_id,
                created_by=user.user_id,
                created_date=datetime.now(timezone.utc),
                last_modified_by=user.user_id,
                last_modified_date=datetime.now(timezone.utc)
            )
            db.session.add(notification)
            db.session.flush()

            # Create delivery record
            delivery = NotificationDeliveries(
                notification_id=notification.id,
                channel='EMAIL',
                status='SENT',
                sent_date=datetime.now(timezone.utc),
                created_by=user.user_id,
                created_date=datetime.now(timezone.utc),
                last_modified_by=user.user_id,
                last_modified_date=datetime.now(timezone.utc)
            )
            db.session.add(delivery)

        except Exception as e:
            print(f"Error sending confirmation email or recording notification: {str(e)}")

        db.session.commit()

        return jsonify({
            "message": "Password reset successful",
            "email_sent": True
        }), 200

    except Exception as e:
        db.session.rollback()
        print(f"Error resetting password: {str(e)}")
        return jsonify({"message": "An error occurred during password reset"}), 500
@app.route('/generate-magic-link', methods=['POST'])
def generate_magic_link():
    try:
        data = request.get_json()
        email = data.get('email')
        first_name = data.get('first_name')
        last_name = data.get('last_name')

        if not all([email, first_name, last_name]):
            return jsonify({"message": "Email, first name, and last name are required"}), 400

        # Find user and validate first name
        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({"message": "Invalid email"}), 401

        # Get customer record if user is a customer
        customer = None
        if user.user_role == 'customer':
            customer = Customer.query.filter_by(user_id=user.user_id).first()

        # Case-insensitive name comparison
        if (user.first_name.lower() != first_name.lower() or
                user.last_name.lower() != last_name.lower()):
            return jsonify({"message": "Invalid user details"}), 401

        # Generate magic link token
        magic_link_token = str(uuid.uuid4())

        # Use expiry time from config
        expiry_minutes = config['SETTINGS']['MAGIC_LINK']['EXPIRY_MINUTES']
        expiry_time = datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)

        # Update user record with magic link info
        user.magic_link = magic_link_token
        user.magic_link_status = 'active'
        user.magic_link_created_date = datetime.now(timezone.utc)
        user.magic_link_expiry_date = expiry_time

        # Generate magic link URL
        magic_link_url = f"{app.config['FRONTEND_URL']}/magic-link/{magic_link_token}"

        # Send email and create notification
        try:
            templates = load_email_templates()
            template = templates['MAGIC_LINK']['SENT']

            html_content = template['HTML'].format(
                user_name=user.first_name,
                magic_link=magic_link_url,
                expiry_minutes=expiry_minutes
            )

            msg = Message(
                subject=template['SUBJECT'],
                recipients=[user.email],
                html=html_content,
                sender=app.config['MAIL_DEFAULT_SENDER']
            )
            mail.send(msg)

            # Create notification record
            notification = CustomerNotifications(
                customer_id=customer.id if customer else None,
                subject="Magic Link Generated",
                message=f"Magic link generated for {user.email}",
                priority="HIGH",
                status="SENT",
                related_entity_type="MAGIC_LINK",
                related_entity_id=user.user_id,
                created_by=user.user_id,
                created_date=datetime.now(timezone.utc),
                last_modified_by=user.user_id,
                last_modified_date=datetime.now(timezone.utc)
            )
            db.session.add(notification)
            db.session.flush()

            # Create delivery record
            delivery = NotificationDeliveries(
                notification_id=notification.id,
                channel='EMAIL',
                status='SENT',
                sent_date=datetime.now(timezone.utc),
                created_by=user.user_id,
                created_date=datetime.now(timezone.utc),
                last_modified_by=user.user_id,
                last_modified_date=datetime.now(timezone.utc)
            )
            db.session.add(delivery)

        except Exception as e:
            print(f"Error sending magic link email or recording notification: {str(e)}")
            # Continue even if email fails

        db.session.commit()

        return jsonify({
            "message": "Magic link sent successfully",
            "expires_in": f"{expiry_minutes} minutes"
        }), 200

    except Exception as e:
        db.session.rollback()
        print(f"Error generating magic link: {str(e)}")
        return jsonify({"message": "An error occurred"}), 500
@app.route('/verify-magic-link/<token>', methods=['POST'])
def verify_magic_link(token):
    try:
        # Find user by magic link token
        user = User.query.filter_by(magic_link=token).first()
        if not user:
            return jsonify({'message': 'Invalid magic link'}), 401

        current_time = datetime.now(timezone.utc)

        # Check link status and expiration
        if user.magic_link_status != 'active':
            return jsonify({'message': 'Magic link is no longer active'}), 401

        if current_time > user.magic_link_expiry_date:
            user.magic_link_status = 'expired'
            db.session.commit()
            return jsonify({'message': 'Magic link has expired'}), 401

        # Get customer record if user is a customer
        customer = None
        if user.user_role == 'customer':
            customer = Customer.query.filter_by(user_id=user.user_id).first()
            if customer:
                customer_taxfinancial = CustomerTaxFinancial.query.filter_by(customer_id=customer.id).first()
                if customer_taxfinancial and customer_taxfinancial.filing_type == 'Married filing jointly':
                    customer_jointmembers = CustomerJointMember.query.filter_by(customer_id=customer.id).first()
                    if customer_jointmembers:
                        spouse_name = customer_jointmembers.member_name
                    else:
                        spouse_name = None
                else:
                    spouse_name = None

        # Mark link as used and update last login
        user.magic_link_status = 'used'
        user.last_login = current_time

        # Generate auth token - exactly as in login
        auth_token = jwt.encode({
            'user_id': user.user_id,
            'user_role': user.user_role,
            'exp': datetime.now(timezone.utc) + timedelta(hours=24)
        }, app.config['SECRET_KEY'], algorithm="HS256")

        db.session.commit()

        # Match login response structure exactly
        response_data = {
            'token': auth_token,
            'user_id': user.user_id,
            'username': user.username,
            'email': user.email,
            'user_role': user.user_role
        }

        # Add customer data if customer - matching login structure
        if user.user_role == 'customer' and customer and customer_taxfinancial:
            response_data.update({
                'customer': {
                    'customer_id': customer.id,
                    'filing_type': customer_taxfinancial.filing_type,
                    'spouse_name': spouse_name
                }
            })

        return jsonify(response_data), 200

    except Exception as e:
        db.session.rollback()
        print(f"Error verifying magic link: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        init_scheduler(app)
        socketio.run(app, debug=True, allow_unsafe_werkzeug=True)
        create_initial_admin_if_not_exists()
    app.run(debug=True)

