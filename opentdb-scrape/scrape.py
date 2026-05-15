import csv
import time
from urllib.parse import unquote
import requests

print("Fetching session token...")
token_url = "https://opentdb.com/api_token.php?command=request"
token = requests.get(token_url).json()["token"]

print("Fetching categories...")
categories_data = requests.get("https://opentdb.com/api_category.php").json()["trivia_categories"]

for category in categories_data:
    cat_id = category["id"]
    cat_name = category["name"].replace(":", " -").replace("/", "-")
    filename = f"{cat_name}.csv"
    
    # Fetch the exact total number of questions for this specific category
    count_url = f"https://opentdb.com/api_count.php?category={cat_id}"
    count_data = requests.get(count_url).json()
    total_questions = count_data["category_question_count"]["total_question_count"]
    
    print(f"\n--- Starting category: {cat_name} ({total_questions} total questions) ---")
    
    # Track how many we have successfully written so far
    questions_fetched = 0
    
    # Keep looping until we've fetched every single question
    while questions_fetched < total_questions:
        
        # Calculate how many questions to ask for
        remaining = total_questions - questions_fetched
        # If there are more than 50 left, ask for 50. Otherwise, ask for exactly what's left.
        amount = 50 if remaining >= 50 else remaining
        
        url = f"https://opentdb.com/api.php?amount={amount}&category={cat_id}&encode=url3986&token={token}"
        response = requests.get(url).json()
        
        if response["response_code"] == 0:
            results = response["results"]
            
            with open(filename, "a+", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                
                # Write header row if the file is empty
                file.seek(0, 2)
                if file.tell() == 0:
                    writer.writerow(["Difficulty", "Type", "Question", "Correct Answer", "Wrong 1", "Wrong 2", "Wrong 3"])
                
                for result in results:
                    q = unquote(result["question"])
                    correct = unquote(result["correct_answer"])
                    incorrect = [unquote(ans) for ans in result["incorrect_answers"]]
                    
                    writer.writerow([result["difficulty"], result["type"], q, correct] + incorrect)
            
            questions_fetched += len(results)
            print(f"Fetched a batch of {len(results)}. Progress for {cat_name}: {questions_fetched}/{total_questions}")
            
            # API requires a 5-second wait between calls
            time.sleep(5.1) 
            
        elif response["response_code"] == 1:
            # Code 1 means the API doesn't have enough questions. 
            # This happens if the total count in the database is slightly inaccurate.
            print(f"API ran dry early. Stopping at {questions_fetched}/{total_questions}.")
            break
            
        elif response["response_code"] == 4:
            print("Token empty for this category.")
            break
            
        elif response["response_code"] == 5:
            print("Hit Rate Limit! Pausing for 10 seconds...")
            time.sleep(10)
            
        else:
            print(f"Unknown error occurred. Response code: {response['response_code']}")
            break

print("\nAll categories completely downloaded!")