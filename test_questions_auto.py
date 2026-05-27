#!/usr/bin/env python3
"""
Improved browser-based chatbot testing automation
Tests all questions through the web interface and verifies document sources
"""

import asyncio
import json
import time
from datetime import datetime
from typing import List, Dict
from playwright.async_api import async_playwright

# Test data: List of (question, expected_document_name)
QUESTIONS = [
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
    ("On what day of the month is my salary credited, and how can I download my digital payslip?", 
     "Compensation & Salary Master Policy"),
    ("Why is my monthly take-home salary significantly lower than the annual CTC mentioned in my offer letter?", 
     "Compensation & Salary Master Policy"),
    ("What is the monthly reimbursement limit for my personal mobile phone and home internet broadband?", 
     "Reimbursement & Expense Policy"),
    ("Can I claim a reimbursement for purchasing a home office chair, keyboard, or monitor under the WFH setup?", 
     "Reimbursement & Expense Policy"),
    ("What is the default hybrid work model split between office days and remote working days?", 
     "Remote Work & VPN Policy"),
    ("How do I connect to the corporate VPN from home, and what gateway address should I use?", 
     "Remote Work & VPN Policy"),
    ("Can I request a second external monitor or upgrade my laptop's RAM?", 
     "IT Equipment & Asset Policy"),
    ("What password strength guidelines must I follow, and how frequently do I need to rotate my corporate login password?", 
     "IT Security Master Policy"),
    ("Can I use my personal smartphone to check work emails, and what is the registration process?", 
     "IT Security Master Policy"),
]

async def extract_document_from_page(page) -> str:
    """Extract the document name from the visible page content"""
    try:
        # Get all text from the page
        page_text = await page.inner_text('body')
        
        # Look for document names in the order they appear
        document_names = [
            "Leave & Time-Off Master Policy",
            "Compensation & Salary Master Policy",
            "Reimbursement & Expense Policy",
            "Remote Work & VPN Policy",
            "IT Equipment & Asset Policy",
            "IT Security Master Policy",
            "Corporate Facilities & Workspace Master Policy"
        ]
        
        # Find which document appears in the current page
        for doc_name in document_names:
            if doc_name in page_text:
                return doc_name
        
        return "Unknown"
    except Exception as e:
        print(f"    Extract error: {e}")
        return "Unknown"

async def run_tests():
    """Run all tests using Playwright"""
    print("=" * 100)
    print("CHATBOT AUTOMATED TESTING SUITE")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 100)
    print()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)  # Headless mode for faster testing
        context = await browser.new_context()
        page = await context.new_page()
        
        print(f"🌐 Opening chatbot...")
        try:
            await page.goto("http://localhost:8000/chatbot.html", timeout=15000)
        except Exception as e:
            print(f"✗ ERROR: Could not open chatbot: {e}")
            await browser.close()
            return
        
        print("✓ Chatbot loaded")
        print()
        
        results = []
        passed = 0
        failed = 0
        
        # Run tests
        for idx, (question, expected_doc) in enumerate(QUESTIONS, 1):
            try:
                # Wait for input to be enabled
                input_field = page.locator('input[id="q"]')
                await input_field.wait_for(timeout=5000)
                
                # Clear and enter question
                await input_field.fill(question)
                print(f"[{idx}/{len(QUESTIONS)}] Q: {question[:65]}...")
                
                # Click send
                send_btn = page.locator('button:has-text("Send")')
                await send_btn.click()
                
                # Wait for response - wait for the chatbot message to appear
                await page.wait_for_selector('text=Veelead Helpdesk here', timeout=20000)
                
                # Wait a bit for full page render
                await page.wait_for_timeout(3000)
                
                # Extract document
                actual_doc = await extract_document_from_page(page)
                
                # Check match
                is_match = expected_doc.lower() in actual_doc.lower()
                
                if is_match:
                    passed += 1
                    print(f"    ✓ PASS - {actual_doc}")
                else:
                    failed += 1
                    print(f"    ✗ FAIL - Got: {actual_doc}")
                
                results.append({
                    "index": idx,
                    "question": question,
                    "expected": expected_doc,
                    "actual": actual_doc,
                    "status": "PASS" if is_match else "FAIL"
                })
                
            except asyncio.TimeoutError:
                failed += 1
                print(f"    ✗ TIMEOUT - Response took too long")
                results.append({
                    "index": idx,
                    "question": question,
                    "expected": expected_doc,
                    "actual": "TIMEOUT",
                    "status": "FAIL"
                })
            except Exception as e:
                failed += 1
                print(f"    ✗ ERROR - {str(e)[:50]}")
                results.append({
                    "index": idx,
                    "question": question,
                    "expected": expected_doc,
                    "actual": f"ERROR: {str(e)}",
                    "status": "FAIL"
                })
        
        await browser.close()
        
        # Print summary
        print()
        print("=" * 100)
        print("RESULTS SUMMARY")
        print("=" * 100)
        print(f"Total:        {len(QUESTIONS)}")
        print(f"Passed:       {passed} ✓")
        print(f"Failed:       {failed} ✗")
        success_rate = (passed/len(QUESTIONS)*100) if QUESTIONS else 0
        print(f"Success Rate: {success_rate:.1f}%")
        print()
        
        # Save results
        output_file = "test_results.json"
        with open(output_file, 'w') as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "total": len(QUESTIONS),
                "passed": passed,
                "failed": failed,
                "success_rate": f"{success_rate:.1f}%",
                "results": results
            }, f, indent=2)
        
        print(f"📄 Results saved to: {output_file}")
        
        # Show detailed results
        print()
        print("=" * 100)
        print("DETAILED RESULTS")
        print("=" * 100)
        for r in results:
            symbol = "✓" if r["status"] == "PASS" else "✗"
            print(f"\n[{symbol}] Q#{r['index']}: {r['question'][:75]}")
            print(f"    Expected: {r['expected']}")
            print(f"    Got:      {r['actual']}")

if __name__ == "__main__":
    try:
        asyncio.run(run_tests())
    except KeyboardInterrupt:
        print("\n\nTest interrupted.")
    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
