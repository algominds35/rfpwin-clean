from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Cookie, Response
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os
import openai
import PyPDF2
import docx
from io import BytesIO
import json
from typing import Dict, Any
from dotenv import load_dotenv
from fastapi.responses import FileResponse, RedirectResponse
import sqlite3
import hashlib
import secrets
from datetime import datetime
import stripe

# Load environment variables
load_dotenv()

app = FastAPI(title="RFPWin - AI Proposal Generator")

# Configure OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

# Configure Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory storage (replace with database later)
rfp_storage = {}

# Initialize database
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            stripe_customer_id TEXT,
            stripe_session_id TEXT,
            plan_name TEXT,
            plan_price TEXT,
            access_token TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# Call this when app starts
init_db()

# Helper functions
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def create_user(email, password, stripe_session_id=None, plan_name=None, plan_price=None):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    
    password_hash = hash_password(password)
    access_token = secrets.token_urlsafe(32)
    
    try:
        cursor.execute('''
            INSERT INTO users (email, password_hash, stripe_session_id, plan_name, plan_price, access_token)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (email, password_hash, stripe_session_id, plan_name, plan_price, access_token))
        conn.commit()
        return access_token
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()

def verify_login(email, password):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    
    password_hash = hash_password(password)
    
    cursor.execute('''
        SELECT access_token FROM users 
        WHERE email = ? AND password_hash = ?
    ''', (email, password_hash))
    
    result = cursor.fetchone()
    
    if result:
        cursor.execute('''
            UPDATE users SET last_login = CURRENT_TIMESTAMP 
            WHERE email = ?
        ''', (email,))
        conn.commit()
    
    conn.close()
    return result[0] if result else None

def verify_token(token):
    if not token:
        return None
        
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT email, plan_name FROM users WHERE access_token = ?
    ''', (token,))
    
    result = cursor.fetchone()
    conn.close()
    
    return result if result else None

# ROUTES
@app.get("/")
async def landing_page():
    """Serve the landing page"""
    return FileResponse('landing.html')

@app.get("/privacy")
async def privacy_policy():
    """Serve the privacy policy page"""
    return FileResponse('privacy.html')

@app.get("/terms")
async def terms_of_service():
    """Serve the terms of service page"""
    return FileResponse('terms.html')

@app.get("/dashboard")
async def dashboard(access_token: str = Cookie(None)):
    """Serve the RFP platform - requires login"""
    user = verify_token(access_token)
    
    if user:
        return FileResponse('index.html')
    else:
        return RedirectResponse(url="/login", status_code=302)

@app.get("/login")
async def login_page():
    return FileResponse('login.html')

@app.get("/create-account")
async def create_account_page():
    return FileResponse('create-account.html')

@app.post("/login")
async def login(request: dict):
    email = request.get('email')
    password = request.get('password')
    
    if not email or not password:
        return {"success": False, "error": "Email and password required"}
    
    access_token = verify_login(email, password)
    
    if access_token:
        return {"success": True, "access_token": access_token}
    else:
        return {"success": False, "error": "Invalid email or password"}

@app.post("/create-account")
async def create_account(request: dict):
    email = request.get('email')
    password = request.get('password')
    plan = request.get('plan', 'Professional')
    session_id = request.get('session_id')
    
    if not email or not password:
        return {"success": False, "error": "Email and password required"}
    
    plan_price = "$97"
    if plan == "Premium":
        plan_price = "$197"
    elif plan == "Enterprise":
        plan_price = "$497"
    
    access_token = create_user(email, password, session_id, plan, plan_price)
    
    if access_token:
        return {"success": True, "access_token": access_token}
    else:
        return {"success": False, "error": "Email already exists"}

@app.get("/payment-success")
async def payment_success(session_id: str):
    try:
        stripe_session = stripe.checkout.Session.retrieve(session_id)
        
        if stripe_session.payment_status == 'paid':
            email = stripe_session.customer_details.email
            
            amount = stripe_session.amount_total / 100
            if amount == 97:
                plan = "Professional"
                price = "$97"
            elif amount == 197:
                plan = "Premium" 
                price = "$197"
            elif amount == 497:
                plan = "Enterprise"
                price = "$497"
            else:
                plan = "Professional"
                price = "$97"
            
            return RedirectResponse(
                url=f"/create-account?email={email}&plan={plan}&price={price}&session_id={session_id}",
                status_code=302
            )
            
    except Exception as e:
        pass
    
    return RedirectResponse(url="/", status_code=302)

# RFP PROCESSING FUNCTIONS
def extract_text_from_pdf(file_content):
    """Extract text from PDF file"""
    try:
        pdf_reader = PyPDF2.PdfReader(BytesIO(file_content))
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text() + "\n"
        return text
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error reading PDF: {str(e)}")

def extract_text_from_docx(file_content):
    """Extract text from DOCX file"""
    try:
        doc = docx.Document(BytesIO(file_content))
        text = ""
        for paragraph in doc.paragraphs:
            text += paragraph.text + "\n"
        return text
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error reading DOCX: {str(e)}")

def parse_rfp_with_ai(rfp_text):
    """Use OpenAI to parse and understand RFP requirements"""
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are an expert at analyzing RFP (Request for Proposal) documents. Extract key information including requirements, evaluation criteria, scope of work, and submission guidelines."},
                {"role": "user", "content": f"Analyze this RFP document and extract the key requirements, evaluation criteria, scope of work, and any specific submission requirements:\n\n{rfp_text[:4000]}"}
            ],
            max_tokens=1000,
            temperature=0.3
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"AI analysis unavailable: {str(e)}"

def generate_proposal_with_ai(rfp_analysis, company_name, company_description, industry="", experience=""):
    """Generate a comprehensive 3-5 page proposal using multiple AI calls"""
    try:
        # Section 1: Executive Summary & Company Overview
        executive_prompt = f"""You are a professional proposal writer. Based on this RFP analysis and company info, write a compelling Executive Summary and Company Overview (2-3 paragraphs each).

RFP ANALYSIS: {rfp_analysis}
COMPANY: {company_name} - {company_description}
INDUSTRY: {industry}
EXPERIENCE: {experience}

Write:
1. EXECUTIVE SUMMARY - Compelling overview highlighting why you're the best choice
2. COMPANY OVERVIEW & QUALIFICATIONS - Detailed company background, experience, and credentials"""

        # Section 2: Technical Approach
        technical_prompt = f"""Based on this RFP analysis, write a detailed Technical Approach & Methodology section:

RFP ANALYSIS: {rfp_analysis}
COMPANY CAPABILITIES: {company_description}

Include detailed technical approach, methodology, risk mitigation, and quality assurance."""

        # Section 3: Team & Timeline
        team_prompt = f"""Write a comprehensive Team & Resources and Project Timeline section:

COMPANY: {company_name} - {company_description}
RFP REQUIREMENTS: {rfp_analysis}

Include team structure, qualifications, timeline, and resource allocation."""

        # Section 4: Value Proposition
        value_prompt = f"""Write a compelling Value Proposition and Conclusion section:

COMPANY STRENGTHS: {company_description}
RFP NEEDS: {rfp_analysis}

Include unique value proposition, cost-benefit analysis, and strong closing."""

        # Generate all sections
        sections = []
        prompts = [executive_prompt, technical_prompt, team_prompt, value_prompt]
        section_titles = [
            "EXECUTIVE SUMMARY & COMPANY OVERVIEW",
            "TECHNICAL APPROACH & METHODOLOGY", 
            "TEAM, RESOURCES & PROJECT TIMELINE",
            "VALUE PROPOSITION & CONCLUSION"
        ]

        for i, prompt in enumerate(prompts):
            response = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are an expert proposal writer with 20+ years of experience winning government and corporate contracts."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1000,
                temperature=0.7
            )
            
            section_content = response.choices[0].message.content
            sections.append(f"\n\n{'='*50}\n{section_titles[i]}\n{'='*50}\n\n{section_content}")

        # Combine all sections
        full_proposal = f"""PROPOSAL RESPONSE
Submitted to: [Client Name]
Submitted by: {company_name}
Date: {datetime.now().strftime("%B %d, %Y")}

{'='*60}""" + "".join(sections) + f"""

{'='*60}

This proposal demonstrates {company_name}'s commitment to delivering exceptional results.

Best regards,
{company_name} Team"""

        return full_proposal

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI proposal generation failed: {str(e)}")

# RFP PROCESSING ROUTES
@app.get("/health")
def health():
    return {"status": "healthy", "service": "RFPWin API"}

@app.post("/upload-rfp")
async def upload_rfp(file: UploadFile = File(...)):
    """Upload and parse RFP document with AI analysis"""
    if not file.filename.endswith(('.pdf', '.docx', '.txt')):
        raise HTTPException(status_code=400, detail="Unsupported file type. Please upload PDF, DOCX, or TXT files.")
    
    try:
        # Read file content
        file_content = await file.read()
        
        # Extract text based on file type
        if file.filename.endswith('.pdf'):
            extracted_text = extract_text_from_pdf(file_content)
        elif file.filename.endswith('.docx'):
            extracted_text = extract_text_from_docx(file_content)
        else:  # .txt
            extracted_text = file_content.decode('utf-8')
        
        # Analyze RFP with AI
        ai_analysis = parse_rfp_with_ai(extracted_text)
        
        # Store in memory (use database in production)
        file_id = f"rfp_{len(rfp_storage) + 1}"
        rfp_storage[file_id] = {
            "filename": file.filename,
            "extracted_text": extracted_text,
            "ai_analysis": ai_analysis
        }
        
        return {
            "message": f"RFP {file.filename} uploaded and analyzed successfully",
            "file_id": file_id,
            "analysis_preview": ai_analysis[:200] + "..." if len(ai_analysis) > 200 else ai_analysis
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")

@app.post("/generate-proposal")
async def generate_proposal(
    file_id: str = Form(...),
    company_name: str = Form(...), 
    company_description: str = Form(...),
    industry: str = Form(""),
    experience: str = Form("")
):
    """Generate AI-powered proposal based on RFP and company data"""
    
    # Get RFP data
    if file_id not in rfp_storage:
        raise HTTPException(status_code=404, detail="RFP not found. Please upload an RFP document first.")
    
    rfp_data = rfp_storage[file_id]
    
    try:
        # Generate proposal using AI
        proposal_content = generate_proposal_with_ai(
            rfp_data["ai_analysis"],
            company_name,
            company_description,
            industry,
            experience
        )
        
        return {
            "message": "Proposal generated successfully",
            "proposal_content": proposal_content,
            "company_name": company_name,
            "rfp_filename": rfp_data["filename"]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating proposal: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
