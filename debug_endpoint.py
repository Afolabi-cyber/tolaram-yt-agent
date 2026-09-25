from server import app
import traceback

with app.test_client() as client:
    print("Sending GET request to /api/stats via Test Client...")
    try:
        response = client.get("/api/stats")
        print(f"Status Code: {response.status_code}")
        print("Response Data:")
        print(response.get_data(as_text=True))
    except Exception as e:
        print("CRITICAL FLASK EXCEPTION CAUGHT:")
        traceback.print_exc()
