"""営業リスト生成API - Vercel Serverless Function"""

import json
import os
from http.server import BaseHTTPRequestHandler
import anthropic


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))

        api_key = body.get("api_key", "").strip()
        if not api_key:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "APIキーが設定されていません"}).encode())
            return

        product_info = body.get("product_info", "")
        num_leads = body.get("num_leads", 20)
        detail_level = body.get("detail_level", "detailed")
        industry_filter = body.get("industry_filter", [])
        company_size = body.get("company_size", [])
        additional_request = body.get("additional_request", "")

        if detail_level == "simple":
            columns_instruction = """
各営業先について以下の情報をJSON配列で返してください:
- company_name: 会社名
- industry: 業種
- reason: この企業が営業先として最適な理由"""
        elif detail_level == "standard":
            columns_instruction = """
各営業先について以下の情報をJSON配列で返してください:
- company_name: 会社名
- industry: 業種
- department: アプローチすべき担当部署
- approach: 推奨アプローチ方法
- reason: この企業が営業先として最適な理由"""
        else:
            columns_instruction = """
各営業先について以下の情報をJSON配列で返してください:
- company_name: 会社名
- industry: 業種
- company_size: 企業規模（大企業/中堅企業/中小企業/小規模企業）
- department: アプローチすべき担当部署
- key_person_title: キーパーソンの想定役職
- challenge: この企業が抱えていそうな課題
- proposal_point: 提案のポイント
- approach: 推奨アプローチ方法（展示会/テレアポ/DM/紹介/Web問い合わせ等）
- priority: 優先度（A:最優先 / B:高 / C:中）
- reason: この企業が営業先として最適な理由"""

        filters = []
        if industry_filter:
            filters.append(f"業種フィルター: {', '.join(industry_filter)}")
        if company_size:
            filters.append(f"企業規模フィルター: {', '.join(company_size)}")
        if additional_request.strip():
            filters.append(f"追加要望: {additional_request}")
        filter_text = "\n".join(filters) if filters else "特になし"

        prompt = f"""あなたは日本市場に精通したB2B営業戦略の専門家です。
以下の商品・サービス情報を分析し、最も効果的な営業先リストを{num_leads}件生成してください。

## 商品・サービス情報
{product_info}

## フィルター条件
{filter_text}

## 出力形式
{columns_instruction}

## 重要な指示
- 実在する可能性が高い具体的な企業名・団体名を挙げてください（架空の企業は不可）
- 商品の特性と企業のニーズのマッチ度が高い順に並べてください
- 日本国内の企業を対象としてください
- 理由は具体的に、なぜその企業が営業先として有効かを説明してください
- 出力はJSON配列のみ（```json ``` で囲む）で、他のテキストは含めないでください"""

        try:
            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = message.content[0].text

            json_str = response_text
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]

            leads = json.loads(json_str.strip())

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"leads": leads}, ensure_ascii=False).encode())

        except json.JSONDecodeError:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "AIの応答をパースできませんでした", "raw": response_text}, ensure_ascii=False).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode())
