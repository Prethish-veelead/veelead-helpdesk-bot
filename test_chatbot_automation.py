#!/usr/bin/env python3
"""
Automated chatbot testing script
Tests all questions and verifies they return answers from the correct documents
"""

import requests
import json
import time
from typing import List, Dict, Tuple
from datetime import datetime

# Configuration
API_BASE = "http://localhost:8000"
SEARCH_URL = f"{API_BASE}/search.json"
API_KEY = "veelead-secure-9f83jsdf9832@"

# Test data: List of (question, expected_document_name)
QUESTIONS = [
    # Leave & Time-Off Master Policy
    ("How many days of Earned Leave (EL), Casual Leave (CL), and Sick Leave (SL) do I get in a calendar year?", 
     "Leave & Time-Off Master Policy"),
    ("I am a mid-year joiner. How will my leave balance be prorated?", 
     "Leave & Time-Off Master Policy"),
    ("Can I take Casual Leave for more than 3 consecutive days?", 
     "Leave & Time-Off Master Policy"),
    ("Do I need to submit a medical certificate if I take Sick Leave, and from what day is it mandatory?", 
     "Leave & Time-Off Master Policy"),
    ("What are the advance notice period requirements for planning a long vacation using my Earned Leave?", 
     "Leave & Time-Off Master Policy"),
    ("Can I apply for a half-day leave, and what leave types can I use for it?", 
     "Leave & Time-Off Master Policy"),
    ("I fell sick suddenly and couldn't access the system. How many days do I have to file a backdated leave request after returning?", 
     "Leave & Time-Off Master Policy"),
    ("Can I combine my Casual Leave (CL) and Earned Leave (EL) together for a single continuous absence?", 
     "Leave & Time-Off Master Policy"),
    ("What is the \"mandatory minimum usage rule\" for Earned Leaves, and what happens if I don't use them?", 
     "Leave & Time-Off Master Policy"),
    ("How many unused Earned Leaves can I carry forward to the next calendar year, and what happens to the remaining balance?", 
     "Leave & Time-Off Master Policy"),
    ("Does the company follow a \"Sandwich Leave Policy\" for weekends and public holidays falling between my leave dates?", 
     "Leave & Time-Off Master Policy"),
    
    # Compensation & Salary Master Policy
    ("On what day of the month is my salary credited, and how can I download my digital payslip?", 
     "Compensation & Salary Master Policy"),
    ("Why is my monthly take-home salary significantly lower than the annual CTC mentioned in my offer letter?", 
     "Compensation & Salary Master Policy"),
    ("What is the formula used to calculate my year-end leave encashment, and which salary component is it based on?", 
     "Leave & Time-Off Master Policy"),  # This one actually comes from Leave policy
    ("Am I eligible to receive a salary advance, and what is the recovery process?", 
     "Compensation & Salary Master Policy"),
    ("What is the eligibility criterion for receiving Gratuity, and what formula is used to calculate it upon separation?", 
     "Compensation & Salary Master Policy"),
    ("What is the timeline for the final financial credit of my Full & Final (F&F) settlement after my last working day?", 
     "Compensation & Salary Master Policy"),
    
    # Reimbursement & Expense Policy
    ("What is the monthly reimbursement limit for my personal mobile phone and home internet broadband?", 
     "Reimbursement & Expense Policy"),
    ("Can I claim a reimbursement for purchasing a home office chair, keyboard, or monitor under the WFH setup?", 
     "Reimbursement & Expense Policy"),
    ("What are the annual financial limits for claiming professional books, learning certifications, and gym memberships?", 
     "Reimbursement & Expense Policy"),
    ("Do I need to provide a physical original bill for expense claims, or are digital scans accepted?", 
     "Reimbursement & Expense Policy"),
    ("From what invoice value is a proper GST tax invoice mandatory for processing my reimbursement claim?", 
     "Reimbursement & Expense Policy"),
    ("What is the deadline to submit an expense claim after the transaction is made?", 
     "Reimbursement & Expense Policy"),
    ("I am planning a family trip. How does the Leave Travel Allowance (LTA) block-year system work for tax exemptions?", 
     "Reimbursement & Expense Policy"),
    
    # Remote Work & VPN Policy
    ("What is the default hybrid work model split between office days and remote working days?", 
     "Remote Work & VPN Policy"),
    ("How do I connect to the corporate VPN from home, and what gateway address should I use?", 
     "Remote Work & VPN Policy"),
    ("What are the mandatory \"core hours\" during which I must be online and responsive on Slack/Teams when working remotely?", 
     "Remote Work & VPN Policy"),
    ("My home internet is running slow. What are the minimum upload and download speeds required to work from home?", 
     "Remote Work & VPN Policy"),
    ("Am I allowed to work from a local coffee shop or public café using public Wi-Fi?", 
     "Remote Work & VPN Policy"),
    
    # IT Equipment & Asset Policy
    ("Can I request a second external monitor or upgrade my laptop's RAM?", 
     "IT Equipment & Asset Policy"),
    ("What happens if I accidentally spill liquid on my corporate laptop or lose it? Am I financially liable for the replacement?", 
     "IT Equipment & Asset Policy"),
    
    # IT Security Master Policy
    ("What password strength guidelines must I follow, and how frequently do I need to rotate my corporate login password?", 
     "IT Security Master Policy"),
    ("Can I use my personal smartphone to check work emails, and what is the registration process?", 
     "IT Security Master Policy"),
    ("I think I accidentally clicked on a phishing link in an unexpected email. What immediate steps should I take?", 
     "IT Security Master Policy"),
    
    # Corporate Facilities & Workspace Master Policy
    ("I lost my physical smart access ID badge. What should I do immediately, and is there a replacement fee?", 
     "Corporate Facilities & Workspace Master Policy"),
    ("Can I bring family members or external friends to the office floors for lunch or a quick visit?", 
     "Corporate Facilities & Workspace Master Policy"),
]

def query_chatbot(question: str, category: str = None) -> Dict:
    """Query the chatbot API"""
    try:
        params = {"q": question}
        if category:
            params["category"] = category
        
        response = requests.get(
            SEARCH_URL,
            params=params,
            headers={"x-api-key": API_KEY},
            timeout=10
        )
        
        # Check if response is 404 - endpoint might not exist
        if response.status_code == 404:
            return {"error": f"404: Endpoint not found at {SEARCH_URL}", "status_code": 404}
        
        response.raise_for_status()
        return response.json()
    except requests.exceptions.ConnectionError as e:
        return {"error": f"Connection refused: {str(e)}", "status_code": "connection_error"}
    except Exception as e:
        return {"error": str(e), "status_code": getattr(response, 'status_code', None) if 'response' in locals() else "unknown"}

def extract_document_name(response: Dict) -> str:
    """Extract document name from chatbot response"""
    if "error" in response:
        return f"ERROR: {response['error']}"
    
    # Check if there are sources in the response
    if "sources" in response:
        if isinstance(response["sources"], list) and len(response["sources"]) > 0:
            source = response["sources"][0]
            if "source_metadata" in source:
                return source["source_metadata"].get("file_name", "Unknown")
            elif "file_name" in source:
                return source["file_name"]
    
    return "Unknown"

def run_tests():
    """Run all tests"""
    print("=" * 100)
    print("CHATBOT AUTOMATION TEST SUITE")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 100)
    print()
    
    # Check server connectivity first
    print("🔍 Checking server connectivity...")
    try:
        response = requests.get(f"{API_BASE}/health", timeout=5)
        print(f"✓ Server is running at {API_BASE}")
        print(f"  Health status: {response.status_code}")
    except requests.exceptions.ConnectionError:
        print(f"✗ ERROR: Cannot connect to server at {API_BASE}")
        print(f"  Make sure the server is running: uvicorn app:app --reload --port 8000")
        return
    except Exception as e:
        print(f"✗ ERROR checking server: {e}")
        return
    
    print()
    
    results = []
    passed = 0
    failed = 0
    
    for idx, (question, expected_doc) in enumerate(QUESTIONS, 1):
        print(f"[{idx}/{len(QUESTIONS)}] Testing: {question[:70]}...")
        
        # Query the chatbot
        response = query_chatbot(question)
        actual_doc = extract_document_name(response)
        
        # Check if it matches
        is_match = expected_doc.lower() in actual_doc.lower()
        status = "✓ PASS" if is_match else "✗ FAIL"
        
        if is_match:
            passed += 1
        else:
            failed += 1
        
        result = {
            "index": idx,
            "question": question,
            "expected_document": expected_doc,
            "actual_document": actual_doc,
            "status": "PASS" if is_match else "FAIL"
        }
        results.append(result)
        
        print(f"  {status}")
        print(f"  Expected: {expected_doc}")
        print(f"  Got:      {actual_doc}")
        
        if not is_match:
            print(f"  Response Keys: {list(response.keys())}")
        print()
        
        # Small delay between requests
        time.sleep(0.5)
    
    # Print summary
    print("=" * 100)
    print("TEST SUMMARY")
    print("=" * 100)
    print(f"Total Tests:  {len(QUESTIONS)}")
    print(f"Passed:       {passed} ✓")
    print(f"Failed:       {failed} ✗")
    print(f"Success Rate: {(passed/len(QUESTIONS)*100):.1f}%")
    print(f"Completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Print detailed results
    print("=" * 100)
    print("DETAILED RESULTS")
    print("=" * 100)
    
    for result in results:
        status_symbol = "✓" if result["status"] == "PASS" else "✗"
        print(f"\n[{status_symbol}] Q#{result['index']}: {result['question'][:80]}")
        print(f"    Expected: {result['expected_document']}")
        print(f"    Got:      {result['actual_document']}")
    
    # Save results to JSON file
    output_file = "test_results.json"
    with open(output_file, 'w') as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "total": len(QUESTIONS),
            "passed": passed,
            "failed": failed,
            "success_rate": f"{(passed/len(QUESTIONS)*100):.1f}%",
            "results": results
        }, f, indent=2)
    
    print(f"\n✓ Results saved to: {output_file}")
    print("=" * 100)

if __name__ == "__main__":
    try:
        run_tests()
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user.")
    except Exception as e:
        print(f"Error running tests: {e}")
        import traceback
        traceback.print_exc()
