#!/usr/bin/env python3
"""
Browser-based chatbot testing automation using Playwright
Tests all questions through the web interface and verifies document sources
"""

import asyncio
import json
from datetime import datetime
from typing import List, Dict, Tuple
from playwright.async_api import async_playwright, expect

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

async def extract_document_from_page(page) -> str:
    """Extract the document name from the 'View source chunks' section"""
    try:
        # Wait for the first source chunk to appear
        await page.wait_for_selector('div:has-text("Leave & Time-Off Master Policy"), div:has-text("Compensation & Salary Master Policy"), div:has-text("Reimbursement & Expense Policy"), div:has-text("Remote Work & VPN Policy"), div:has-text("IT Equipment & Asset Policy"), div:has-text("IT Security Master Policy"), div:has-text("Corporate Facilities & Workspace Master Policy")', timeout=10000)
        
        # Get all source elements
        sources_text = await page.inner_text('body')
        
        # Extract the first source document name
        for doc_name in [
            "Leave & Time-Off Master Policy",
            "Compensation & Salary Master Policy",
            "Reimbursement & Expense Policy",
            "Remote Work & VPN Policy",
            "IT Equipment & Asset Policy",
            "IT Security Master Policy",
            "Corporate Facilities & Workspace Master Policy"
        ]:
            if doc_name in sources_text:
                return doc_name
        
        return "Unknown"
    except:
        return "Unknown"

async def run_tests():
    """Run all tests using Playwright"""
    print("=" * 100)
    print("CHATBOT AUTOMATION TEST SUITE (Browser-based)")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 100)
    print()
    
    async with async_playwright() as p:
        # Launch browser
        browser = await p.chromium.launch(headless=False)  # Set to False to see the browser
        context = await browser.new_context()
        page = await context.new_page()
        
        # Navigate to chatbot
        print(f"🌐 Opening chatbot at http://localhost:8000/chatbot.html...")
        try:
            await page.goto("http://localhost:8000/chatbot.html", timeout=10000)
        except Exception as e:
            print(f"✗ ERROR: Could not open chatbot: {e}")
            await browser.close()
            return
        
        print("✓ Chatbot page loaded")
        print()
        
        results = []
        passed = 0
        failed = 0
        
        # Run tests
        for idx, (question, expected_doc) in enumerate(QUESTIONS, 1):
            print(f"[{idx}/{len(QUESTIONS)}] Testing: {question[:70]}...")
            
            try:
                # Find input field and enter question
                input_field = page.locator('input[placeholder*="Type your question"]')
                await input_field.fill(question)
                
                # Click send button
                send_btn = page.locator('button:has-text("Send")')
                await send_btn.click()
                
                # Wait for response - look for bot message
                await page.wait_for_selector('div:has-text("Veelead Helpdesk")', timeout=15000)
                
                # Wait a bit for the response to fully render
                await page.wait_for_timeout(2000)
                
                # Extract document name from response
                actual_doc = await extract_document_from_page(page)
                
                # Check if match
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
                print()
                
            except Exception as e:
                print(f"  ✗ ERROR: {e}")
                failed += 1
                results.append({
                    "index": idx,
                    "question": question,
                    "expected_document": expected_doc,
                    "actual_document": f"ERROR: {str(e)}",
                    "status": "FAIL"
                })
                print()
        
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
        
        # Save results
        output_file = "test_results_browser.json"
        with open(output_file, 'w') as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "total": len(QUESTIONS),
                "passed": passed,
                "failed": failed,
                "success_rate": f"{(passed/len(QUESTIONS)*100):.1f}%",
                "results": results
            }, f, indent=2)
        
        print(f"✓ Results saved to: {output_file}")
        print("=" * 100)
        
        # Keep browser open for inspection
        print("\nBrowser will remain open for inspection. Close it manually when done.")
        await page.wait_for_timeout(300000)  # Wait 5 minutes
        
        await browser.close()

if __name__ == "__main__":
    try:
        asyncio.run(run_tests())
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user.")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
