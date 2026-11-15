"""Fixed call-control prompts; no medicine facts or search logic."""
PROMPTS = {
    "en": {
        "language": "Please speak in Hindi or English. Press seven for Hindi or eight for English.",
        "wait": "One moment, I am checking your question.",
        "retry": "I could not complete that request. Please say the medicine name again.",
        "idle": "I am listening. Please say the medicine name or your question.",
        "goodbye": "I have not heard a question. Goodbye. You can call again.",
    },
    "hi": {
        "language": "कृपया हिंदी या अंग्रेज़ी में बोलें। हिंदी के लिए सात या अंग्रेज़ी के लिए आठ दबाएँ।",
        "wait": "एक क्षण, मैं आपका सवाल जाँच रही हूँ।",
        "retry": "आपका सवाल पूरा नहीं हो पाया। कृपया दवा का नाम फिर से बोलें।",
        "idle": "मैं सुन रही हूँ। कृपया दवा का नाम या अपना सवाल बोलें।",
        "goodbye": "आपका सवाल सुनाई नहीं दिया। नमस्ते। आप फिर कॉल कर सकते हैं।",
    },
}
