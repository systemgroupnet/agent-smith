from flask import Flask, render_template, request, jsonify
from main import process_mr
import asyncio

app = Flask(__name__,static_folder='assets', static_url_path='/assets')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/review', methods=['POST'])
def review():
    data = request.get_json()
    mr = data.get('mr')
    
    if mr is None:
        return jsonify({'error': 'MR is required'}), 400
    
    asyncio.run(process_mr(mr))
    return jsonify({'result': 'DONE'})

if __name__ == '__main__':
    app.run(debug=True)