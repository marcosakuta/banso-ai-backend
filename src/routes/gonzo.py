from flask import Blueprint, jsonify, request, Response, stream_template
from datetime import datetime, timedelta
import openai
import os
import uuid
import json
import time

gonzo_bp = Blueprint('gonzo', __name__)

# セッション管理用の辞書（本番環境ではRedisなどを使用）
sessions = {}

# セッションの有効期限（30分）
SESSION_TIMEOUT = timedelta(minutes=30)

# OpenAI クライアントの初期化
MOCK_MODE = os.getenv('MOCK_MODE', 'false').lower() == 'true'
if not MOCK_MODE:
    api_key = os.getenv('OPENAI_API_KEY', 'test-key')
    if api_key == 'test-key' or api_key == 'your-openai-api-key-here':
        MOCK_MODE = True
        print("⚠️ APIキーが設定されていません。モックモードで動作します。")
    else:
        try:
            client = openai.OpenAI(
                api_key=api_key,
                base_url=os.getenv('OPENAI_API_BASE', 'https://api.openai.com/v1')
            )
            # APIキーの有効性をテスト
            test_response = client.models.list()
            print(f"✅ OpenAI API接続成功: {len(list(test_response))} モデル利用可能")
        except Exception as e:
            print(f"⚠️ OpenAI API接続エラー: {str(e)}")
            if 'insufficient_quota' in str(e):
                print("💡 ヒント: OpenAIダッシュボードで課金設定を確認してください。")
                print("   https://platform.openai.com/account/billing/overview")
            MOCK_MODE = True
            print("モックモードで動作します。")

# 構造化プロンプト（アクションプロンプト1）
STRUCTURE_PROMPT = """【目的】ユーザーの質問を意味ベースで解釈し、以下の6要素に分解してください。
さらに、内容に応じて最大3つまでのタグを付与してください。

【出力形式】
- 原文：<<ユーザーの質問文>>
- 要約：一文に要約（口語OK）
- 意図仮説：質問者の関心・目的・想定シナリオ（複数可）
- 対象範囲：業務分野・制度・行動・判断などの主対象
- 前提条件：質問文に内在する仮定・思い込み・制約
- 推奨問い返し：Gonzoが自然に返すならどんな問い？
- タグ：以下のカテゴリから該当するものを最大3つ

【タグ候補カテゴリ】
#KPI設計 #補助金 #DX支援 #目標と手段のズレ #構造化困難 #感情由来 
#制度誤解 #ヒント待ち #火が弱い #行動前提なし #BanSo操作 #Slackで議論中 
#てこ構造 #今すぐ判断したい #応援を求めている

【質問文】
{user_message}"""

# Gonzo応答プロンプト（システムプロンプト2 + アクションプロンプト2の統合版）
GONZO_PROMPT = """あなたは「Gonzo」という仮想人格を持つ、実務派の戦略コンサルタント型AIです。

【性格・背景】
- 相手の中にある"行動のスイッチ"を押すような問いを重視
- 「伴走者」として同じ視点で問題解決に取り組む
- 冷静さと熱さ、構造と感情の両方を扱える
- 形式よりも意味、速さよりも"納得できる温度"を大切にする
- Slack/Zoomで話すようなテンポで応答

【専門性】
- Lean Six Sigma Black Belt
- ISO主任審査員（複数分野）
- 中小企業診断士、IPAレベル2以上
- 中小企業支援、DX、補助金、KPI、BI等に精通

【応答ルール】
1. 回答前に相手の発言を「〜ということであれば…」と要約
2. 1-2段落で簡潔に応答
3. 必要に応じて問い返しを1つ添える
4. タグを1-2個自然に埋め込む
5. 確定的でない場合は「〜かもしれません」と断定を避ける

【口癖】
- 「それ、スイッチ入ってる？」
- 「それ、"てこ"になる話か？」
- 「問いに納得がないと、誰も動かないよ？」

【象徴語の翻訳】
- 「スイッチ」＝相手の中にある納得・動機・実行の引き金
- 「伴走者」＝支援対象者と対等に立ち、一緒に試行錯誤する関係性

【過去の対話履歴】
{conversation_history}"""

def clean_expired_sessions():
    """期限切れのセッションを削除"""
    current_time = datetime.now()
    expired_sessions = [
        session_id for session_id, session_data in sessions.items()
        if current_time - session_data['last_activity'] > SESSION_TIMEOUT
    ]
    for session_id in expired_sessions:
        del sessions[session_id]

def get_or_create_session(session_id=None):
    """セッションを取得または作成"""
    clean_expired_sessions()
    
    if session_id and session_id in sessions:
        # 既存セッションの更新
        sessions[session_id]['last_activity'] = datetime.now()
        return session_id, sessions[session_id]
    else:
        # 新しいセッションの作成
        new_session_id = str(uuid.uuid4())
        sessions[new_session_id] = {
            'messages': [],
            'last_activity': datetime.now(),
            'created_at': datetime.now()
        }
        return new_session_id, sessions[new_session_id]

@gonzo_bp.route('/chat', methods=['POST'])
def chat():
    """チャット機能のメインエンドポイント"""
    try:
        data = request.json
        user_message = data.get('message', '').strip()
        session_id = data.get('session_id')
        
        if not user_message:
            return jsonify({'error': 'メッセージが空です'}), 400
        
        # セッション管理
        session_id, session_data = get_or_create_session(session_id)
        
        # Skip structure analysis for speed
        structured_analysis = ""
        
        # 対話履歴の準備
        conversation_history = ""
        if session_data['messages']:
            recent_messages = session_data['messages'][-6:]  # 最新6件の対話
            for msg in recent_messages:
                conversation_history += f"ユーザー: {msg['user']}\nGonzo: {msg['gonzo']}\n\n"
        
        # Gonzo応答の生成
        if MOCK_MODE:
            # モックモード: サンプル応答を返す
            gonzo_reply = f"""なるほど、「{user_message}」ということであれば、
まず状況を整理させていただきたいと思います。

あなたが求めているのは、具体的な解決策でしょうか、それとも問題の本質を一緒に探ることでしょうか？

それ、スイッチ入ってる？と私はよく聞きますが、本当に動きたいと思えるような「納得」はありますか？

#DX支援 の観点から言えば、テクノロジーは手段でしかありません。
大事なのは「何のために」それを使うのか、ですよね。

もう少し、あなたの状況や背景を教えていただけると、より具体的な提案ができるかもしれません。"""
        else:
            gonzo_response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": GONZO_PROMPT.format(
                        conversation_history=conversation_history
                    )},
                    {"role": "user", "content": user_message}
                ],
                temperature=1
            )
            
            gonzo_reply = gonzo_response.choices[0].message.content
        
        # セッションに対話を保存
        session_data['messages'].append({
            'user': user_message,
            'gonzo': gonzo_reply,
            'structured_analysis': structured_analysis,
            'timestamp': datetime.now().isoformat()
        })
        
        # 古い対話を削除（最新20件のみ保持）
        if len(session_data['messages']) > 20:
            session_data['messages'] = session_data['messages'][-20:]
        
        return jsonify({
            'response': gonzo_reply,
            'session_id': session_id,
            'structured_analysis': structured_analysis,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        return jsonify({'error': f'エラーが発生しました: {str(e)}'}), 500

@gonzo_bp.route('/chat/stream', methods=['POST'])
def chat_stream():
    """ストリーミングチャット機能"""
    try:
        data = request.json
        user_message = data.get('message', '').strip()
        session_id = data.get('session_id')
        
        if not user_message:
            return jsonify({'error': 'メッセージが空です'}), 400
        
        # セッション管理
        session_id, session_data = get_or_create_session(session_id)
        
        def generate_stream():
            try:
                # 画像データを取得
                images = data.get('images', [])
                
                # Skip structure analysis for speed - go directly to response
                structured_analysis = ""
                
                # 対話履歴の準備
                conversation_history = ""
                if session_data['messages']:
                    recent_messages = session_data['messages'][-6:]
                    for msg in recent_messages:
                        conversation_history += f"ユーザー: {msg['user']}\nGonzo: {msg['gonzo']}\n\n"
                
                # Gonzo応答の生成（高速化のためステータスも省略）
                
                full_response = ""
                
                if MOCK_MODE:
                    # モックモード: 人間らしいストリーミングをシミュレート
                    mock_response = f"""なるほど、「{user_message}」ということであれば、
まず状況を整理させていただきたいと思います。

あなたが求めているのは、具体的な解決策でしょうか、それとも問題の本質を一緒に探ることでしょうか？

それ、スイッチ入ってる？と私はよく聞きますが、本当に動きたいと思えるような「納得」はありますか？

#DX支援 の観点から言えば、テクノロジーは手段でしかありません。
大事なのは「何のために」それを使うのか、ですよね。

もう少し、あなたの状況や背景を教えていただけると、より具体的な提案ができるかもしれません。"""
                    
                    # 人間らしいストリーミングをシミュレート
                    import time
                    import random
                    
                    # 文章を句読点で分割してチャンク化
                    import re
                    sentences = re.split(r'([。！？])', mock_response)
                    sentences = [''.join(sentences[i:i+2]) for i in range(0, len(sentences), 2) if sentences[i]]
                    
                    for sent_idx, sentence in enumerate(sentences):
                        # 文の開始前に少し考える時間
                        if sent_idx > 0 and random.random() < 0.3:
                            time.sleep(random.uniform(0.3, 0.8))
                        
                        # 文を小さなチャンクに分割（2-5文字ずつ）
                        chunks = []
                        i = 0
                        while i < len(sentence):
                            # 読点や改行で区切る
                            if sentence[i] in '、\n':
                                if chunks and chunks[-1]:
                                    chunks.append(sentence[i])
                                    i += 1
                                    continue
                            
                            # 通常の文字グループ（2-5文字）
                            chunk_size = random.randint(2, 5)
                            chunk = sentence[i:i+chunk_size]
                            chunks.append(chunk)
                            i += chunk_size
                        
                        # チャンクを出力
                        for chunk in chunks:
                            full_response += chunk
                            yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                            
                            # タイピング速度の変化
                            if chunk in '、。！？\n':  # 句読点の後は少し間を置く
                                time.sleep(random.uniform(0.15, 0.3))
                            elif random.random() < 0.1:  # 時々考える
                                time.sleep(random.uniform(0.1, 0.2))
                            elif random.random() < 0.3:  # ゆっくり
                                time.sleep(random.uniform(0.04, 0.08))
                            else:  # 通常速度
                                time.sleep(random.uniform(0.02, 0.04))
                else:
                    # 画像がある場合はGPT-4o、ない場合はGPT-3.5-turbo
                    
                    # ユーザーメッセージを構築
                    user_content = []
                    if user_message:
                        user_content.append({"type": "text", "text": user_message})
                    
                    # 画像を追加
                    for img in images:
                        if img.get('url'):
                            user_content.append({
                                "type": "image_url",
                                "image_url": {"url": img['url']}
                            })
                    
                    # コンテンツがない場合はテキストのみ
                    if not user_content:
                        user_content = user_message
                    
                    try:
                        # 画像がある場合はVision対応モデル、ない場合はGPT-4o-miniを使用（高速）
                        model_name = "gpt-4o" if images else "gpt-4o-mini"
                        
                        # Use streaming for all models now
                        if False:
                            response = client.chat.completions.create(
                                model=model_name,
                                messages=[
                                    {"role": "system", "content": GONZO_PROMPT.format(
                                        structured_analysis=structured_analysis,
                                        conversation_history=conversation_history
                                    )},
                                    {"role": "user", "content": user_message}
                                ],
                                temperature=1
                            )
                            full_response = response.choices[0].message.content
                            
                            # Simulate streaming for consistent UI experience
                            import time
                            import random
                            # Send the entire response at once for maximum speed
                            yield f"data: {json.dumps({'type': 'content', 'content': full_response})}\n\n"
                        else:
                            stream = client.chat.completions.create(
                                model=model_name,
                                messages=[
                                    {"role": "system", "content": GONZO_PROMPT.format(
                                        structured_analysis=structured_analysis,
                                        conversation_history=conversation_history
                                    )},
                                    {"role": "user", "content": user_content}
                                ],
                                temperature=1,
                                stream=True,
                                max_completion_tokens=1000
                            )
                            import time
                            import random
                            buffer = ""
                            
                            for chunk in stream:
                                if chunk.choices[0].delta.content is not None:
                                    content = chunk.choices[0].delta.content
                                    buffer += content
                                    
                                    # バッファに文字が溜まったら出力（大きなチャンクで高速）
                                    while len(buffer) >= 20:
                                        output_chunk = buffer[:50] if len(buffer) >= 50 else buffer
                                        buffer = buffer[len(output_chunk):]
                                        
                                        full_response += output_chunk
                                        yield f"data: {json.dumps({'type': 'content', 'content': output_chunk})}\n\n"
                                        
                                        # Ultra-fast streaming with almost no delay
                                        time.sleep(0.001)
                            
                            # 残りのバッファを出力
                            if buffer:
                                full_response += buffer
                                yield f"data: {json.dumps({'type': 'content', 'content': buffer})}\n\n"
                    
                    except Exception as api_error:
                        if 'insufficient_quota' in str(api_error):
                            # クォータエラーの場合、インテリジェントなフォールバック応答を生成
                            raise Exception("insufficient_quota")
                        else:
                            raise api_error
                
                # セッションに対話を保存
                session_data['messages'].append({
                    'user': user_message,
                    'gonzo': full_response,
                    'structured_analysis': structured_analysis,
                    'timestamp': datetime.now().isoformat()
                })
                
                # 古い対話を削除（最新20件のみ保持）
                if len(session_data['messages']) > 20:
                    session_data['messages'] = session_data['messages'][-20:]
                
                # 完了通知
                yield f"data: {json.dumps({'type': 'complete', 'session_id': session_id, 'timestamp': datetime.now().isoformat()})}\n\n"
                
            except Exception as e:
                error_msg = str(e)
                if 'insufficient_quota' in error_msg:
                    # APIキーのクォータエラーの場合、モックモードで応答
                    mock_response = f"""申し訳ございません。現在、APIの利用制限に達しているようです。

「{user_message}」についてのご質問ですね。

通常であれば、より詳細な分析と提案をさせていただくところですが、
現在システムの制限により、簡易的な応答となってしまいます。

それでも、あなたの問題解決に向けて一緒に考えていきたいと思います。
具体的にどのような課題や背景があるのか、もう少し詳しく教えていただけますか？"""
                    
                    for chunk in mock_response.split('\n'):
                        if chunk:
                            yield f"data: {json.dumps({'type': 'content', 'content': chunk + '\n'})}\n\n"
                            import time
                            time.sleep(0.05)
                    
                    # セッションに保存
                    session_data['messages'].append({
                        'user': user_message,
                        'gonzo': mock_response,
                        'structured_analysis': '(API制限により分析不可)',
                        'timestamp': datetime.now().isoformat()
                    })
                    
                    yield f"data: {json.dumps({'type': 'complete', 'session_id': session_id, 'timestamp': datetime.now().isoformat()})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'message': f'エラーが発生しました: {error_msg}'})}\n\n"
        
        return Response(
            generate_stream(),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Content-Type',
            }
        )
        
    except Exception as e:
        return jsonify({'error': f'エラーが発生しました: {str(e)}'}), 500

@gonzo_bp.route('/sessions/<session_id>', methods=['GET'])
def get_session(session_id):
    """セッション情報の取得"""
    clean_expired_sessions()
    
    if session_id not in sessions:
        return jsonify({'error': 'セッションが見つかりません'}), 404
    
    session_data = sessions[session_id]
    return jsonify({
        'session_id': session_id,
        'messages': session_data['messages'],
        'created_at': session_data['created_at'].isoformat(),
        'last_activity': session_data['last_activity'].isoformat()
    })

@gonzo_bp.route('/sessions/<session_id>', methods=['DELETE'])
def delete_session(session_id):
    """セッションの削除"""
    if session_id in sessions:
        del sessions[session_id]
        return jsonify({'message': 'セッションが削除されました'})
    else:
        return jsonify({'error': 'セッションが見つかりません'}), 404

@gonzo_bp.route('/sessions', methods=['GET'])
def list_sessions():
    """アクティブなセッション一覧の取得"""
    clean_expired_sessions()
    
    session_list = []
    for session_id, session_data in sessions.items():
        session_list.append({
            'session_id': session_id,
            'created_at': session_data['created_at'].isoformat(),
            'last_activity': session_data['last_activity'].isoformat(),
            'message_count': len(session_data['messages'])
        })
    
    return jsonify({'sessions': session_list})

@gonzo_bp.route('/health', methods=['GET'])
def health_check():
    """ヘルスチェック"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'active_sessions': len(sessions)
    })

