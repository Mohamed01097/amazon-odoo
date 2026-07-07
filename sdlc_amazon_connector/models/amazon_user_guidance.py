from odoo import models, fields


class AmazonUserGuidance(models.TransientModel):
    _name = 'amazon.user.guidance'
    _description = 'Amazon Connector User Guidance'

    guidance_content = fields.Html(
        string='User Guide',
        default=lambda self: self._get_guidance_html(),
        sanitize=False,
    )

    ai_summary_content = fields.Html(
        string='AI Features Summary',
        default=lambda self: self._get_ai_summary_html(),
        sanitize=False,
    )

    # ------------------------------------------------------------------
    #  GUIDANCE HTML
    # ------------------------------------------------------------------
    def _get_guidance_html(self):
        return """
        <div style="font-family: 'Segoe UI', Roboto, Arial, sans-serif; max-width: 1100px; margin: 0 auto; color: #333;">

            <!-- ═══════════════ HEADER ═══════════════ -->
            <div style="background: linear-gradient(135deg, #FF9900 0%, #FF6600 100%); border-radius: 16px; padding: 40px; margin-bottom: 30px; color: #fff; text-align: center;">
                <h1 style="margin: 0; font-size: 32px; font-weight: 700; color: #fff;">&#128230; Odoo Amazon Connector</h1>
                <p style="margin: 10px 0 0; font-size: 16px; opacity: .9; color: #fff;">Complete SP-API Integration with AI-Powered Automation &mdash; v3.0</p>
            </div>

            <!-- ═══════════════ GETTING STARTED ═══════════════ -->
            <div style="background: #f8f9fa; border-radius: 12px; padding: 30px; margin-bottom: 24px; border-left: 5px solid #FF9900;">
                <h2 style="margin-top: 0; color: #232F3E;">&#127919; Getting Started</h2>
                <ol style="line-height: 2; font-size: 15px;">
                    <li><b>Create an Instance</b> &mdash; Go to <i>Configuration &rarr; Instances</i> and add your Amazon Seller credentials. See the step-by-step walkthrough below.</li>
                    <li><b>Test Connection</b> &mdash; Click <em>"Test Connection"</em> on the instance form to verify your API credentials.</li>
                    <li><b>Import Products</b> &mdash; Use <i>Catalog &rarr; Import / Map Products</i> to bulk-map or import your Amazon SKUs.</li>
                    <li><b>Sync Orders</b> &mdash; Hit <em>"Import Orders"</em> on the instance or let the scheduled action handle it automatically.</li>
                    <li><b>Enable AI</b> &mdash; In <i>Configuration &rarr; Settings</i>, enable AI features and configure your AI provider (OpenAI, Claude, Gemini, or Groq).</li>
                </ol>
            </div>

            <!-- ═══════════════ INSTANCE CREATION WALKTHROUGH ═══════════════ -->
            <div style="background: #fff; border-radius: 12px; padding: 30px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06);">
                <h2 style="margin-top: 0; color: #232F3E;">&#128295; How to Create an Amazon Instance (Step-by-Step)</h2>
                <p style="font-size: 14px; color: #555;">Before filling the form, gather these seven values from three places: Amazon Seller Central, the Amazon Developer Console, and AWS IAM.</p>

                <div style="background: #FFFBF5; border-radius: 10px; padding: 20px; margin-bottom: 16px; border-left: 4px solid #FF9900;">
                    <h3 style="margin-top: 0; color: #FF9900;">Step 1 &mdash; Seller ID &amp; Marketplace ID</h3>
                    <ul style="line-height: 1.8; font-size: 14px;">
                        <li><b>Seller Central</b> &rarr; top-right gear icon &rarr; <i>Account Info</i> &rarr; <i>Your Merchant Token</i>. This is the <b>Seller ID</b> (looks like <code>A2Q1S22EXAMPLE</code>, 14 chars, starts with <code>A</code>).</li>
                        <li><b>Marketplace ID</b> is fixed per country. Common values:
                            <table style="margin-top: 8px; border-collapse: collapse; font-size: 13px;">
                                <tr style="background: #f8f9fa;"><th style="padding: 6px 12px; text-align: left;">Country</th><th style="padding: 6px 12px; text-align: left;">Marketplace ID</th><th style="padding: 6px 12px; text-align: left;">Region</th></tr>
                                <tr><td style="padding: 6px 12px;">US</td><td style="padding: 6px 12px; font-family: monospace;">ATVPDKIKX0DER</td><td style="padding: 6px 12px;">na</td></tr>
                                <tr><td style="padding: 6px 12px;">Canada</td><td style="padding: 6px 12px; font-family: monospace;">A2EUQ1WTGCTBG2</td><td style="padding: 6px 12px;">na</td></tr>
                                <tr><td style="padding: 6px 12px;">Mexico</td><td style="padding: 6px 12px; font-family: monospace;">A1AM78C64UM0Y8</td><td style="padding: 6px 12px;">na</td></tr>
                                <tr><td style="padding: 6px 12px;">UK</td><td style="padding: 6px 12px; font-family: monospace;">A1F83G8C2ARO7P</td><td style="padding: 6px 12px;">eu</td></tr>
                                <tr><td style="padding: 6px 12px;">Germany</td><td style="padding: 6px 12px; font-family: monospace;">A1PA6795UKMFR9</td><td style="padding: 6px 12px;">eu</td></tr>
                                <tr><td style="padding: 6px 12px;">France</td><td style="padding: 6px 12px; font-family: monospace;">A13V1IB3VIYZZH</td><td style="padding: 6px 12px;">eu</td></tr>
                                <tr><td style="padding: 6px 12px;">Italy</td><td style="padding: 6px 12px; font-family: monospace;">APJ6JRA9NG5V4</td><td style="padding: 6px 12px;">eu</td></tr>
                                <tr><td style="padding: 6px 12px;">Spain</td><td style="padding: 6px 12px; font-family: monospace;">A1RKKUPIHCS9HS</td><td style="padding: 6px 12px;">eu</td></tr>
                                <tr><td style="padding: 6px 12px;">India</td><td style="padding: 6px 12px; font-family: monospace;">A21TJRUUN4KGV</td><td style="padding: 6px 12px;">eu</td></tr>
                                <tr><td style="padding: 6px 12px;">Japan</td><td style="padding: 6px 12px; font-family: monospace;">A1VC38T7YXB528</td><td style="padding: 6px 12px;">fe</td></tr>
                                <tr><td style="padding: 6px 12px;">Australia</td><td style="padding: 6px 12px; font-family: monospace;">A39IBJ37TRP1C6</td><td style="padding: 6px 12px;">fe</td></tr>
                            </table>
                            <span style="font-size: 12px; color: #777;">Tip: the <b>Region</b> field auto-corrects from the Marketplace ID when you save.</span>
                        </li>
                    </ul>
                </div>

                <div style="background: #F0F8FF; border-radius: 10px; padding: 20px; margin-bottom: 16px; border-left: 4px solid #0073BB;">
                    <h3 style="margin-top: 0; color: #0073BB;">Step 2 &mdash; LWA Client ID, Client Secret &amp; Refresh Token (Amazon Developer Console)</h3>
                    <ol style="line-height: 1.8; font-size: 14px;">
                        <li>Open <i>Seller Central &rarr; Apps &amp; Services &rarr; Develop Apps</i> (you must be enrolled as a developer).</li>
                        <li>Create a new <b>SP-API app</b> (or open your existing one).</li>
                        <li><b>Client ID</b> and <b>Client Secret</b> are shown on the app page under "LWA credentials". The Client ID looks like <code>amzn1.application-oa2-client.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx</code>.</li>
                        <li>To get the <b>Refresh Token</b>, click <em>Authorize</em> on your app, complete the Login with Amazon flow with the Seller account that owns the marketplace, and copy the token returned. It starts with <code>Atzr|</code> and is very long. Treat it like a password.</li>
                    </ol>
                </div>

                <div style="background: #FFF0F5; border-radius: 10px; padding: 20px; margin-bottom: 16px; border-left: 4px solid #C62828;">
                    <h3 style="margin-top: 0; color: #C62828;">Step 3 &mdash; AWS Fields Are Optional</h3>
                    <ol style="line-height: 1.8; font-size: 14px;">
                        <li>Current SP-API requests use the Login With Amazon access token only.</li>
                        <li><b>AWS Access Key</b> and <b>AWS Secret Key</b> remain on the form only for older records and backward compatibility.</li>
                        <li>You can leave both AWS fields empty. Test Connection and sync actions do not require them.</li>
                    </ol>
                </div>

                <div style="background: #F0FFF0; border-radius: 10px; padding: 20px; border-left: 4px solid #2E7D32;">
                    <h3 style="margin-top: 0; color: #2E7D32;">Step 4 &mdash; Fill the form and Test Connection</h3>
                    <ol style="line-height: 1.8; font-size: 14px;">
                        <li><i>Configuration &rarr; Instances &rarr; New</i>.</li>
                        <li>Give the instance a meaningful <b>Name</b> (e.g. "Acme US Marketplace").</li>
                        <li>Paste the required SP-API credentials: <b>Seller ID</b>, <b>Marketplace ID</b>, <b>Refresh Token</b>, <b>Client ID</b>, and <b>Client Secret</b>. Optionally set <b>FBA Warehouse</b>, <b>FBM Warehouse</b>, and <b>Default Currency</b> &mdash; these are used by order/stock sync but not by Test Connection.</li>
                        <li>Click <b>Save manually</b> (the cloud icon).</li>
                        <li>Click <b>Test Connection</b>. A green "Connection successful" notification confirms credentials are valid.</li>
                        <li>Optionally configure AI in the same form: pick a provider, paste its API key, click <b>Test AI Connection</b>.</li>
                    </ol>
                </div>
            </div>

            <!-- ═══════════════ TROUBLESHOOTING ═══════════════ -->
            <div style="background: #fff; border-radius: 12px; padding: 30px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06); border-left: 5px solid #C62828;">
                <h2 style="margin-top: 0; color: #C62828;">&#128737;&#65039; Troubleshooting</h2>

                <div style="margin-bottom: 16px; padding: 16px; background: #FFF8F8; border-radius: 8px;">
                    <h4 style="margin: 0 0 8px; color: #C62828;">UserError: Missing required fields: ...</h4>
                    <p style="font-size: 13px; color: #555; margin: 0;">One of the required SP-API fields (Seller ID, Marketplace ID, Refresh Token, Client ID, Client Secret) is blank or whitespace-only. Fill it and Save before clicking any action button. AWS Access Key and AWS Secret Key are optional.</p>
                </div>

                <div style="margin-bottom: 16px; padding: 16px; background: #FFF8F8; border-radius: 8px;">
                    <h4 style="margin: 0 0 8px; color: #C62828;">Amazon HTTP 400: invalid_grant &mdash; User may have revoked or didn't grant the permission</h4>
                    <p style="font-size: 13px; color: #555; margin: 0;">The refresh token is wrong, expired, or has been revoked by the seller. Re-run the LWA authorization flow on your SP-API app and paste the new refresh token. Refresh tokens do not expire on a schedule but are invalidated if the seller revokes the app or you regenerate the LWA secret.</p>
                </div>

                <div style="margin-bottom: 16px; padding: 16px; background: #FFF8F8; border-radius: 8px;">
                    <h4 style="margin: 0 0 8px; color: #C62828;">Amazon HTTP 403 &mdash; Access denied / Unauthorized</h4>
                    <p style="font-size: 13px; color: #555; margin: 0;">Your SP-API app does not have the required role for the operation (e.g. <i>Product Listing</i> for Sync Products, <i>Finance &amp; Accounting</i> for Settlements). Add the role in the Developer Console, re-authorize, and get a new refresh token.</p>
                </div>

                <div style="margin-bottom: 16px; padding: 16px; background: #FFF8F8; border-radius: 8px;">
                    <h4 style="margin: 0 0 8px; color: #C62828;">Amazon HTTP 429 &mdash; Rate limit / Throttled</h4>
                    <p style="font-size: 13px; color: #555; margin: 0;">The connector retries with exponential backoff. If you see this in logs, you've exceeded Amazon's per-operation rate. Reduce auto-sync frequency on the instance form or wait a few minutes.</p>
                </div>

                <div style="margin-bottom: 16px; padding: 16px; background: #FFF8F8; border-radius: 8px;">
                    <h4 style="margin: 0 0 8px; color: #C62828;">Region mismatch</h4>
                    <p style="font-size: 13px; color: #555; margin: 0;">If you set the Region manually and it disagrees with your Marketplace ID, the connector will <b>auto-correct it for you</b> on Test Connection and on every Sync action (logged at INFO level). Always trust the Marketplace ID; the Region field is derived.</p>
                </div>

                <div style="margin-bottom: 16px; padding: 16px; background: #FFF8F8; border-radius: 8px;">
                    <h4 style="margin: 0 0 8px; color: #C62828;">RPC_ERROR / _csv.Error: new-line character seen in unquoted field</h4>
                    <p style="font-size: 13px; color: #555; margin: 0;">Fixed in v3.0+. Caused by stray carriage returns inside product titles (e.g. Mac-origin spreadsheets). The current code normalizes line endings before parsing. If you see this error, you are running an old build &mdash; update the module and restart Odoo with <code>-u sdlc_amazon_connector</code>.</p>
                </div>

                <div style="padding: 16px; background: #FFF8F8; border-radius: 8px;">
                    <h4 style="margin: 0 0 8px; color: #C62828;">Where to look when something fails</h4>
                    <p style="font-size: 13px; color: #555; margin: 0;">Open <i>Reports &rarr; Sync Logs</i> for the detailed request/response of every operation. The most recent failed entry usually contains the precise Amazon error message and request ID, which you can include in any support ticket.</p>
                </div>
            </div>

            <!-- ═══════════════ CORE FEATURES ═══════════════ -->
            <div style="background: #fff; border-radius: 12px; padding: 30px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06);">
                <h2 style="margin-top: 0; color: #232F3E;">&#128736;&#65039; Core Features</h2>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">

                    <div style="background: #FFF8F0; border-radius: 10px; padding: 20px;">
                        <h3 style="margin-top: 0; color: #FF9900;">&#128666; Order Management</h3>
                        <ul style="line-height: 1.8; padding-left: 20px;">
                            <li>Import FBM &amp; FBA orders automatically</li>
                            <li>Auto-create Odoo Sale Orders &amp; Deliveries</li>
                            <li>Track delivery status (Pending &rarr; Shipped &rarr; Delivered)</li>
                            <li>Cancel order sync</li>
                            <li>Export tracking numbers to Amazon</li>
                        </ul>
                    </div>

                    <div style="background: #F0F8FF; border-radius: 10px; padding: 20px;">
                        <h3 style="margin-top: 0; color: #0073BB;">&#128230; Product Catalog</h3>
                        <ul style="line-height: 1.8; padding-left: 20px;">
                            <li>Full product sync with ASIN, SKU, FNSKU</li>
                            <li>12 product categories with specialized fields</li>
                            <li>Variation support (Color, Size, Material, etc.)</li>
                            <li>Bulk CSV/Excel import &amp; mapping</li>
                            <li>9-image gallery per product</li>
                        </ul>
                    </div>

                    <div style="background: #F0FFF0; border-radius: 10px; padding: 20px;">
                        <h3 style="margin-top: 0; color: #2E7D32;">&#128178; Pricing &amp; Stock</h3>
                        <ul style="line-height: 1.8; padding-left: 20px;">
                            <li>Bidirectional price sync (Odoo &harr; Amazon)</li>
                            <li>Bidirectional stock sync (Odoo &harr; Amazon)</li>
                            <li>FBA live stock monitoring</li>
                            <li>Bulk price &amp; stock updates via file</li>
                        </ul>
                    </div>

                    <div style="background: #FFF0F5; border-radius: 10px; padding: 20px;">
                        <h3 style="margin-top: 0; color: #C62828;">&#128176; Accounting</h3>
                        <ul style="line-height: 1.8; padding-left: 20px;">
                            <li>Settlement report download &amp; reconciliation</li>
                            <li>Auto-create reimbursement vendor bills</li>
                            <li>VCS tax invoice processing</li>
                            <li>Invoice upload to Amazon</li>
                        </ul>
                    </div>
                </div>
            </div>

            <!-- ═══════════════ FBA SECTION ═══════════════ -->
            <div style="background: #fff; border-radius: 12px; padding: 30px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06);">
                <h2 style="margin-top: 0; color: #232F3E;">&#128230; FBA Operations</h2>
                <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px;">
                    <div style="text-align: center; padding: 20px; background: #f8f9fa; border-radius: 10px;">
                        <div style="font-size: 36px;">&#128666;</div>
                        <h4>Inbound Shipments</h4>
                        <p style="font-size: 13px; color: #666;">Create &amp; manage FBA shipment plans using the v2024 API. Track labels, carriers, and receiving status.</p>
                    </div>
                    <div style="text-align: center; padding: 20px; background: #f8f9fa; border-radius: 10px;">
                        <div style="font-size: 36px;">&#128465;&#65039;</div>
                        <h4>Removal Orders</h4>
                        <p style="font-size: 13px; color: #666;">Request returns or disposal of FBA inventory. Track shipped &amp; cancelled quantities.</p>
                    </div>
                    <div style="text-align: center; padding: 20px; background: #f8f9fa; border-radius: 10px;">
                        <div style="font-size: 36px;">&#127760;</div>
                        <h4>Multi-Channel (MCF)</h4>
                        <p style="font-size: 13px; color: #666;">Fulfill non-Amazon orders via FBA. Auto-populate from Odoo SOs with tracking.</p>
                    </div>
                </div>
            </div>

            <!-- ═══════════════ RETURNS & REPORTS ═══════════════ -->
            <div style="background: #fff; border-radius: 12px; padding: 30px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06);">
                <h2 style="margin-top: 0; color: #232F3E;">&#128203; Reports &amp; Returns</h2>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                    <div>
                        <h3 style="color: #FF9900;">Return Management</h3>
                        <ul style="line-height: 1.8;">
                            <li>Download FBA return reports automatically</li>
                            <li>Auto-create credit notes linked to original invoices</li>
                            <li>Track return reasons &amp; status</li>
                        </ul>
                    </div>
                    <div>
                        <h3 style="color: #FF9900;">Seller Ratings &amp; Feedback</h3>
                        <ul style="line-height: 1.8;">
                            <li>Download seller feedback reports</li>
                            <li>Average rating aggregation</li>
                            <li>Respond to customer feedback</li>
                        </ul>
                    </div>
                </div>
            </div>

            <!-- ═══════════════ AUTOMATION ═══════════════ -->
            <div style="background: #232F3E; border-radius: 12px; padding: 30px; margin-bottom: 24px; color: #fff;">
                <h2 style="margin-top: 0; color: #FF9900;">&#9881;&#65039; Automation &amp; Scheduling</h2>
                <p style="color: #ccc;">The connector includes <b>15 scheduled actions</b> (disabled by default). Enable them on your Instance form:</p>
                <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; margin-top: 16px;">
                    <div style="background: rgba(255,255,255,.08); padding: 12px; border-radius: 8px;">
                        <b style="color: #FF9900;">Orders</b><br/><span style="font-size: 13px;">Every 15-30 min</span>
                    </div>
                    <div style="background: rgba(255,255,255,.08); padding: 12px; border-radius: 8px;">
                        <b style="color: #FF9900;">Products</b><br/><span style="font-size: 13px;">Every 6 hours</span>
                    </div>
                    <div style="background: rgba(255,255,255,.08); padding: 12px; border-radius: 8px;">
                        <b style="color: #FF9900;">Prices</b><br/><span style="font-size: 13px;">Every 4 hours</span>
                    </div>
                    <div style="background: rgba(255,255,255,.08); padding: 12px; border-radius: 8px;">
                        <b style="color: #FF9900;">Stock</b><br/><span style="font-size: 13px;">Every 2-4 hours</span>
                    </div>
                    <div style="background: rgba(255,255,255,.08); padding: 12px; border-radius: 8px;">
                        <b style="color: #FF9900;">Settlements</b><br/><span style="font-size: 13px;">Daily</span>
                    </div>
                    <div style="background: rgba(255,255,255,.08); padding: 12px; border-radius: 8px;">
                        <b style="color: #FF9900;">Smart Alerts</b><br/><span style="font-size: 13px;">Every 4 hours</span>
                    </div>
                </div>
            </div>

            <!-- ═══════════════ SYNC LOGS ═══════════════ -->
            <div style="background: #f8f9fa; border-radius: 12px; padding: 30px; margin-bottom: 24px; border-left: 5px solid #0073BB;">
                <h2 style="margin-top: 0; color: #232F3E;">&#128202; Monitoring &amp; Logs</h2>
                <p style="font-size: 15px;">Every sync operation is logged with full details:</p>
                <ul style="line-height: 1.8;">
                    <li><b>Sync Reports</b> &mdash; High-level summaries grouped by date &amp; operation type</li>
                    <li><b>Sync Logs (Raw)</b> &mdash; Detailed request/response data for debugging</li>
                    <li><b>Real-time Notifications</b> &mdash; Bus notifications for sync status updates</li>
                    <li><b>Auto-cleanup</b> &mdash; Old logs purged automatically</li>
                </ul>
            </div>

            <!-- ═══════════════ MARKETPLACE SUPPORT ═══════════════ -->
            <div style="background: #fff; border-radius: 12px; padding: 30px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06);">
                <h2 style="margin-top: 0; color: #232F3E;">&#127760; Supported Marketplaces</h2>
                <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px;">
                    <div>
                        <h4 style="color: #FF9900;">&#127464;&#127462; North America</h4>
                        <ul style="font-size: 13px; line-height: 1.8;">
                            <li>US &bull; Canada &bull; Mexico &bull; Brazil</li>
                        </ul>
                    </div>
                    <div>
                        <h4 style="color: #FF9900;">&#127466;&#127482; Europe</h4>
                        <ul style="font-size: 13px; line-height: 1.8;">
                            <li>UK &bull; DE &bull; FR &bull; IT &bull; ES &bull; NL &bull; SE &bull; PL &bull; BE &bull; TR &bull; AE &bull; SA &bull; EG &bull; IN</li>
                        </ul>
                    </div>
                    <div>
                        <h4 style="color: #FF9900;">&#127471;&#127477; Far East</h4>
                        <ul style="font-size: 13px; line-height: 1.8;">
                            <li>JP &bull; AU &bull; SG</li>
                        </ul>
                    </div>
                </div>
            </div>

            <!-- ═══════════════ TIPS ═══════════════ -->
            <div style="background: linear-gradient(135deg, #232F3E 0%, #37475A 100%); border-radius: 12px; padding: 30px; color: #fff;">
                <h2 style="margin-top: 0; color: #FF9900;">&#128161; Pro Tips</h2>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
                    <div style="background: rgba(255,255,255,.06); border-radius: 8px; padding: 16px;">
                        <b style="color: #FF9900;">&#9989; Start Small</b>
                        <p style="font-size: 13px; color: #ccc; margin-bottom: 0;">Import a few products first, test the order flow, then enable automation.</p>
                    </div>
                    <div style="background: rgba(255,255,255,.06); border-radius: 8px; padding: 16px;">
                        <b style="color: #FF9900;">&#128274; Separate Warehouses</b>
                        <p style="font-size: 13px; color: #ccc; margin-bottom: 0;">Use different warehouses for FBA and FBM to keep stock movements clean.</p>
                    </div>
                    <div style="background: rgba(255,255,255,.06); border-radius: 8px; padding: 16px;">
                        <b style="color: #FF9900;">&#129302; AI Provider</b>
                        <p style="font-size: 13px; color: #ccc; margin-bottom: 0;">Groq (free tier) is great for testing. Switch to GPT-4 or Claude for production quality.</p>
                    </div>
                    <div style="background: rgba(255,255,255,.06); border-radius: 8px; padding: 16px;">
                        <b style="color: #FF9900;">&#128202; Check Logs</b>
                        <p style="font-size: 13px; color: #ccc; margin-bottom: 0;">Always review Sync Logs after enabling a new scheduled action to catch issues early.</p>
                    </div>
                </div>
            </div>

        </div>
        """

    # ------------------------------------------------------------------
    #  AI SUMMARY HTML
    # ------------------------------------------------------------------
    def _get_ai_summary_html(self):
        return """
        <div style="font-family: 'Segoe UI', Roboto, Arial, sans-serif; max-width: 1100px; margin: 0 auto; color: #333;">

            <!-- ═══════════════ AI HEADER ═══════════════ -->
            <div style="background: linear-gradient(135deg, #6C3EC1 0%, #9B59B6 50%, #FF9900 100%); border-radius: 16px; padding: 40px; margin-bottom: 30px; color: #fff; text-align: center;">
                <h1 style="margin: 0; font-size: 32px; font-weight: 700; color: #fff;">&#129302; AI-Powered Features</h1>
                <p style="margin: 10px 0 0; font-size: 16px; opacity: .9; color: #fff;">10 AI Features &bull; 4 AI Providers &bull; Complete Automation</p>
            </div>

            <!-- ═══════════════ AI STATS BANNER ═══════════════ -->
            <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 30px;">
                <div style="background: linear-gradient(135deg, #FF6B35, #FF9900); border-radius: 12px; padding: 24px; text-align: center; color: #fff;">
                    <div style="font-size: 36px; font-weight: 800;">10</div>
                    <div style="font-size: 13px; opacity: .9;">AI Features</div>
                </div>
                <div style="background: linear-gradient(135deg, #6C3EC1, #9B59B6); border-radius: 12px; padding: 24px; text-align: center; color: #fff;">
                    <div style="font-size: 36px; font-weight: 800;">4</div>
                    <div style="font-size: 13px; opacity: .9;">AI Providers</div>
                </div>
                <div style="background: linear-gradient(135deg, #2E7D32, #4CAF50); border-radius: 12px; padding: 24px; text-align: center; color: #fff;">
                    <div style="font-size: 36px; font-weight: 800;">3</div>
                    <div style="font-size: 13px; opacity: .9;">AI Categories</div>
                </div>
                <div style="background: linear-gradient(135deg, #0073BB, #29B6F6); border-radius: 12px; padding: 24px; text-align: center; color: #fff;">
                    <div style="font-size: 36px; font-weight: 800;">13</div>
                    <div style="font-size: 13px; opacity: .9;">Smart Alert Types</div>
                </div>
            </div>

            <!-- ═══════════════ AI PROVIDERS ═══════════════ -->
            <div style="background: #fff; border-radius: 12px; padding: 30px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06);">
                <h2 style="margin-top: 0; color: #6C3EC1;">&#9889; Supported AI Providers</h2>
                <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px;">
                    <div style="text-align: center; padding: 20px; background: #f8f9fa; border-radius: 10px; border: 2px solid #eee;">
                        <div style="font-size: 32px;">&#129302;</div>
                        <h4 style="margin: 8px 0 4px;">Groq</h4>
                        <p style="font-size: 12px; color: #666; margin: 0;">LLaMA 3.3 70B<br/>Free tier available</p>
                        <span style="display: inline-block; margin-top: 8px; background: #E8F5E9; color: #2E7D32; padding: 2px 10px; border-radius: 12px; font-size: 11px;">Default</span>
                    </div>
                    <div style="text-align: center; padding: 20px; background: #f8f9fa; border-radius: 10px; border: 2px solid #eee;">
                        <div style="font-size: 32px;">&#127775;</div>
                        <h4 style="margin: 8px 0 4px;">OpenAI</h4>
                        <p style="font-size: 12px; color: #666; margin: 0;">GPT-4 / GPT-4o<br/>Best accuracy</p>
                        <span style="display: inline-block; margin-top: 8px; background: #E3F2FD; color: #0073BB; padding: 2px 10px; border-radius: 12px; font-size: 11px;">Premium</span>
                    </div>
                    <div style="text-align: center; padding: 20px; background: #f8f9fa; border-radius: 10px; border: 2px solid #eee;">
                        <div style="font-size: 32px;">&#128172;</div>
                        <h4 style="margin: 8px 0 4px;">Anthropic Claude</h4>
                        <p style="font-size: 12px; color: #666; margin: 0;">Claude Sonnet 4<br/>Best reasoning</p>
                        <span style="display: inline-block; margin-top: 8px; background: #F3E5F5; color: #6C3EC1; padding: 2px 10px; border-radius: 12px; font-size: 11px;">Premium</span>
                    </div>
                    <div style="text-align: center; padding: 20px; background: #f8f9fa; border-radius: 10px; border: 2px solid #eee;">
                        <div style="font-size: 32px;">&#128142;</div>
                        <h4 style="margin: 8px 0 4px;">Google Gemini</h4>
                        <p style="font-size: 12px; color: #666; margin: 0;">Gemini 2.0 Flash<br/>Fast &amp; affordable</p>
                        <span style="display: inline-block; margin-top: 8px; background: #FFF3E0; color: #E65100; padding: 2px 10px; border-radius: 12px; font-size: 11px;">Value</span>
                    </div>
                </div>
            </div>

            <!-- ═══════════════ AI FEATURES BY CATEGORY ═══════════════ -->

            <!-- Category 1: Content & Listing -->
            <div style="background: #fff; border-radius: 12px; padding: 30px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06);">
                <div style="display: flex; align-items: center; margin-bottom: 20px;">
                    <span style="background: #FF9900; color: #fff; padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 600;">CATEGORY 1</span>
                    <h2 style="margin: 0 0 0 12px; color: #232F3E;">Content &amp; Listing Optimization</h2>
                    <span style="margin-left: auto; background: #FFF3E0; color: #E65100; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600;">3 Features</span>
                </div>

                <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px;">
                    <div style="border: 2px solid #FF9900; border-radius: 12px; padding: 20px; background: #FFFBF5;">
                        <div style="font-size: 28px; margin-bottom: 8px;">&#128221;</div>
                        <h3 style="margin: 0 0 8px; color: #232F3E; font-size: 16px;">AI Listing Generator</h3>
                        <p style="font-size: 13px; color: #555; line-height: 1.6; margin-bottom: 12px;">Generates SEO-optimized titles, 5 bullet points, rich descriptions, backend keywords, and A+ content suggestions.</p>
                        <div style="font-size: 12px; color: #888;"><b>Output:</b> SEO Score 0-100</div>
                        <div style="font-size: 12px; color: #888;"><b>Menu:</b> AI Tools &rarr; Listing Optimiser</div>
                    </div>
                    <div style="border: 2px solid #FF9900; border-radius: 12px; padding: 20px; background: #FFFBF5;">
                        <div style="font-size: 28px; margin-bottom: 8px;">&#128269;</div>
                        <h3 style="margin: 0 0 8px; color: #232F3E; font-size: 16px;">Product Type Detection</h3>
                        <p style="font-size: 13px; color: #555; line-height: 1.6; margin-bottom: 12px;">AI analyzes product details to detect the correct Amazon product type. Prevents the notorious error 4000003.</p>
                        <div style="font-size: 12px; color: #888;"><b>Output:</b> Product Type ID</div>
                        <div style="font-size: 12px; color: #888;"><b>Trigger:</b> Auto on product export</div>
                    </div>
                    <div style="border: 2px solid #FF9900; border-radius: 12px; padding: 20px; background: #FFFBF5;">
                        <div style="font-size: 28px; margin-bottom: 8px;">&#11088;</div>
                        <h3 style="margin: 0 0 8px; color: #232F3E; font-size: 16px;">Review Analysis</h3>
                        <p style="font-size: 13px; color: #555; line-height: 1.6; margin-bottom: 12px;">Analyzes customer reviews to extract positive/negative themes, sentiment scores, top complaints, and improvement suggestions.</p>
                        <div style="font-size: 12px; color: #888;"><b>Output:</b> Sentiment Score 0-1</div>
                        <div style="font-size: 12px; color: #888;"><b>Menu:</b> AI Tools &rarr; Review Analysis</div>
                    </div>
                </div>
            </div>

            <!-- Category 2: Pricing & Profitability -->
            <div style="background: #fff; border-radius: 12px; padding: 30px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06);">
                <div style="display: flex; align-items: center; margin-bottom: 20px;">
                    <span style="background: #2E7D32; color: #fff; padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 600;">CATEGORY 2</span>
                    <h2 style="margin: 0 0 0 12px; color: #232F3E;">Pricing &amp; Profitability</h2>
                    <span style="margin-left: auto; background: #E8F5E9; color: #2E7D32; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600;">3 Features</span>
                </div>

                <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px;">
                    <div style="border: 2px solid #2E7D32; border-radius: 12px; padding: 20px; background: #F5FFF5;">
                        <div style="font-size: 28px; margin-bottom: 8px;">&#128178;</div>
                        <h3 style="margin: 0 0 8px; color: #232F3E; font-size: 16px;">AI Price Optimizer</h3>
                        <p style="font-size: 13px; color: #555; line-height: 1.6; margin-bottom: 12px;">Suggests optimal pricing with competitor analysis, Buy Box strategy, and 5 pricing modes (Competitive, Penetration, Premium, Value, Undercut).</p>
                        <div style="font-size: 12px; color: #888;"><b>Output:</b> Confidence Score + Strategy</div>
                        <div style="font-size: 12px; color: #888;"><b>Menu:</b> AI Tools &rarr; AI Pricing</div>
                    </div>
                    <div style="border: 2px solid #2E7D32; border-radius: 12px; padding: 20px; background: #F5FFF5;">
                        <div style="font-size: 28px; margin-bottom: 8px;">&#128202;</div>
                        <h3 style="margin: 0 0 8px; color: #232F3E; font-size: 16px;">Profit Calculator</h3>
                        <p style="font-size: 13px; color: #555; line-height: 1.6; margin-bottom: 12px;">AI-driven margin analysis including Amazon fees, FBA costs, GST, packaging. Calculates per-unit profit, ROI, and 30-day projections.</p>
                        <div style="font-size: 12px; color: #888;"><b>Output:</b> Profit Margin % &amp; ROI %</div>
                        <div style="font-size: 12px; color: #888;"><b>Menu:</b> AI Tools &rarr; Profit Calculator</div>
                    </div>
                    <div style="border: 2px solid #2E7D32; border-radius: 12px; padding: 20px; background: #F5FFF5;">
                        <div style="font-size: 28px; margin-bottom: 8px;">&#127919;</div>
                        <h3 style="margin: 0 0 8px; color: #232F3E; font-size: 16px;">Competitor Monitor</h3>
                        <p style="font-size: 13px; color: #555; line-height: 1.6; margin-bottom: 12px;">Track competitor prices and market positioning. Receive alerts when competitors undercut your pricing or win the Buy Box.</p>
                        <div style="font-size: 12px; color: #888;"><b>Output:</b> Competitor Pricing Data</div>
                        <div style="font-size: 12px; color: #888;"><b>Menu:</b> AI Tools &rarr; Competitor Monitor</div>
                    </div>
                </div>
            </div>

            <!-- Category 3: Forecasting & Intelligence -->
            <div style="background: #fff; border-radius: 12px; padding: 30px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06);">
                <div style="display: flex; align-items: center; margin-bottom: 20px;">
                    <span style="background: #6C3EC1; color: #fff; padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 600;">CATEGORY 3</span>
                    <h2 style="margin: 0 0 0 12px; color: #232F3E;">Forecasting &amp; Intelligence</h2>
                    <span style="margin-left: auto; background: #F3E5F5; color: #6C3EC1; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600;">4 Features</span>
                </div>

                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
                    <div style="border: 2px solid #6C3EC1; border-radius: 12px; padding: 20px; background: #FAF5FF;">
                        <div style="font-size: 28px; margin-bottom: 8px;">&#128200;</div>
                        <h3 style="margin: 0 0 8px; color: #232F3E; font-size: 16px;">Demand Forecasting</h3>
                        <p style="font-size: 13px; color: #555; line-height: 1.6; margin-bottom: 12px;">AI predicts 30/60/90-day sales with reorder points, stockout risk assessment (Low/Medium/High), seasonality detection, and auto purchase order creation.</p>
                        <div style="font-size: 12px; color: #888;"><b>Output:</b> Forecast + Reorder Quantity</div>
                        <div style="font-size: 12px; color: #888;"><b>Menu:</b> AI Tools &rarr; Demand Forecast</div>
                    </div>
                    <div style="border: 2px solid #6C3EC1; border-radius: 12px; padding: 20px; background: #FAF5FF;">
                        <div style="font-size: 28px; margin-bottom: 8px;">&#128153;</div>
                        <h3 style="margin: 0 0 8px; color: #232F3E; font-size: 16px;">Product Health Score</h3>
                        <p style="font-size: 13px; color: #555; line-height: 1.6; margin-bottom: 12px;">Overall health grade (A-F) combining listing quality, pricing health, inventory health, sales velocity, and review scores into a single 0-100 score.</p>
                        <div style="font-size: 12px; color: #888;"><b>Output:</b> Health Grade A/B/C/D/F</div>
                        <div style="font-size: 12px; color: #888;"><b>Menu:</b> AI Tools &rarr; Product Health Scores</div>
                    </div>
                    <div style="border: 2px solid #6C3EC1; border-radius: 12px; padding: 20px; background: #FAF5FF;">
                        <div style="font-size: 28px; margin-bottom: 8px;">&#128680;</div>
                        <h3 style="margin: 0 0 8px; color: #232F3E; font-size: 16px;">Smart Alert Engine</h3>
                        <p style="font-size: 13px; color: #555; line-height: 1.6; margin-bottom: 12px;">13 alert types: stockout risk, price wars, Buy Box lost, listing suppressed, possible hijack, negative margin, high return rate, policy warnings, and more.</p>
                        <div style="font-size: 12px; color: #888;"><b>Output:</b> Severity-rated Alerts</div>
                        <div style="font-size: 12px; color: #888;"><b>Menu:</b> Alerts &rarr; Active Alerts</div>
                    </div>
                    <div style="border: 2px solid #6C3EC1; border-radius: 12px; padding: 20px; background: #FAF5FF;">
                        <div style="font-size: 28px; margin-bottom: 8px;">&#128172;</div>
                        <h3 style="margin: 0 0 8px; color: #232F3E; font-size: 16px;">AI Chat Assistant</h3>
                        <p style="font-size: 13px; color: #555; line-height: 1.6; margin-bottom: 12px;">Floating chat widget for real-time AI assistance. Ask questions about your Amazon business, get smart action suggestions, and view dashboard stats.</p>
                        <div style="font-size: 12px; color: #888;"><b>Output:</b> Context-aware Responses</div>
                        <div style="font-size: 12px; color: #888;"><b>Access:</b> Chat widget (bottom-right)</div>
                    </div>
                </div>
            </div>

            <!-- ═══════════════ AI FEATURE SUMMARY TABLE ═══════════════ -->
            <div style="background: #fff; border-radius: 12px; padding: 30px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06);">
                <h2 style="margin-top: 0; color: #232F3E;">&#128203; Complete AI Feature Index</h2>
                <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
                    <thead>
                        <tr style="background: #232F3E; color: #fff;">
                            <th style="padding: 12px; text-align: left; border-radius: 8px 0 0 0;">#</th>
                            <th style="padding: 12px; text-align: left;">Feature</th>
                            <th style="padding: 12px; text-align: left;">Type</th>
                            <th style="padding: 12px; text-align: left;">Category</th>
                            <th style="padding: 12px; text-align: left; border-radius: 0 8px 0 0;">Model</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr style="background: #FFFBF5;"><td style="padding: 10px; border-bottom: 1px solid #eee;">1</td><td style="padding: 10px; border-bottom: 1px solid #eee;"><b>AI Listing Generator</b></td><td style="padding: 10px; border-bottom: 1px solid #eee;">&#128221; Content Generation</td><td style="padding: 10px; border-bottom: 1px solid #eee;">Content &amp; Listing</td><td style="padding: 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 12px;">amazon.ai.listing</td></tr>
                        <tr style="background: #fff;"><td style="padding: 10px; border-bottom: 1px solid #eee;">2</td><td style="padding: 10px; border-bottom: 1px solid #eee;"><b>Product Type Detection</b></td><td style="padding: 10px; border-bottom: 1px solid #eee;">&#128269; Classification</td><td style="padding: 10px; border-bottom: 1px solid #eee;">Content &amp; Listing</td><td style="padding: 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 12px;">amazon.product</td></tr>
                        <tr style="background: #FFFBF5;"><td style="padding: 10px; border-bottom: 1px solid #eee;">3</td><td style="padding: 10px; border-bottom: 1px solid #eee;"><b>Review Analysis</b></td><td style="padding: 10px; border-bottom: 1px solid #eee;">&#11088; Sentiment Analysis</td><td style="padding: 10px; border-bottom: 1px solid #eee;">Content &amp; Listing</td><td style="padding: 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 12px;">amazon.review.analysis</td></tr>
                        <tr style="background: #F5FFF5;"><td style="padding: 10px; border-bottom: 1px solid #eee;">4</td><td style="padding: 10px; border-bottom: 1px solid #eee;"><b>AI Price Optimizer</b></td><td style="padding: 10px; border-bottom: 1px solid #eee;">&#128178; Price Optimization</td><td style="padding: 10px; border-bottom: 1px solid #eee;">Pricing &amp; Profitability</td><td style="padding: 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 12px;">amazon.ai.pricing</td></tr>
                        <tr style="background: #fff;"><td style="padding: 10px; border-bottom: 1px solid #eee;">5</td><td style="padding: 10px; border-bottom: 1px solid #eee;"><b>Profit Calculator</b></td><td style="padding: 10px; border-bottom: 1px solid #eee;">&#128202; Financial Analysis</td><td style="padding: 10px; border-bottom: 1px solid #eee;">Pricing &amp; Profitability</td><td style="padding: 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 12px;">amazon.profit.calculator</td></tr>
                        <tr style="background: #F5FFF5;"><td style="padding: 10px; border-bottom: 1px solid #eee;">6</td><td style="padding: 10px; border-bottom: 1px solid #eee;"><b>Competitor Monitor</b></td><td style="padding: 10px; border-bottom: 1px solid #eee;">&#127919; Market Intelligence</td><td style="padding: 10px; border-bottom: 1px solid #eee;">Pricing &amp; Profitability</td><td style="padding: 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 12px;">amazon.competitor.monitor</td></tr>
                        <tr style="background: #FAF5FF;"><td style="padding: 10px; border-bottom: 1px solid #eee;">7</td><td style="padding: 10px; border-bottom: 1px solid #eee;"><b>Demand Forecasting</b></td><td style="padding: 10px; border-bottom: 1px solid #eee;">&#128200; Predictive Analytics</td><td style="padding: 10px; border-bottom: 1px solid #eee;">Forecasting &amp; Intelligence</td><td style="padding: 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 12px;">amazon.demand.forecast</td></tr>
                        <tr style="background: #fff;"><td style="padding: 10px; border-bottom: 1px solid #eee;">8</td><td style="padding: 10px; border-bottom: 1px solid #eee;"><b>Product Health Score</b></td><td style="padding: 10px; border-bottom: 1px solid #eee;">&#128153; Health Scoring</td><td style="padding: 10px; border-bottom: 1px solid #eee;">Forecasting &amp; Intelligence</td><td style="padding: 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 12px;">amazon.product.health</td></tr>
                        <tr style="background: #FAF5FF;"><td style="padding: 10px; border-bottom: 1px solid #eee;">9</td><td style="padding: 10px; border-bottom: 1px solid #eee;"><b>Smart Alert Engine</b></td><td style="padding: 10px; border-bottom: 1px solid #eee;">&#128680; Proactive Monitoring</td><td style="padding: 10px; border-bottom: 1px solid #eee;">Forecasting &amp; Intelligence</td><td style="padding: 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 12px;">amazon.smart.alert</td></tr>
                        <tr style="background: #fff;"><td style="padding: 10px; border-bottom: 1px solid #eee;">10</td><td style="padding: 10px; border-bottom: 1px solid #eee;"><b>AI Chat Assistant</b></td><td style="padding: 10px; border-bottom: 1px solid #eee;">&#128172; Conversational AI</td><td style="padding: 10px; border-bottom: 1px solid #eee;">Forecasting &amp; Intelligence</td><td style="padding: 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 12px;">amazon.ai.chat</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- ═══════════════ HOW TO ENABLE AI ═══════════════ -->
            <div style="background: linear-gradient(135deg, #6C3EC1 0%, #9B59B6 100%); border-radius: 12px; padding: 30px; color: #fff;">
                <h2 style="margin-top: 0; color: #fff;">&#128295; How to Enable AI Features</h2>
                <ol style="line-height: 2.2; font-size: 15px;">
                    <li>Go to <b>Configuration &rarr; Settings</b></li>
                    <li>Enable <b>"Enable AI Features"</b> toggle</li>
                    <li>Select your <b>AI Provider</b> on the Instance form (Groq, OpenAI, Claude, or Gemini)</li>
                    <li>Enter your <b>API Key</b> for the selected provider</li>
                    <li>Click <b>"Test AI Connection"</b> to verify</li>
                    <li>Optionally enable: <b>Auto Pricing</b>, <b>Auto Listing</b>, <b>Auto Forecast</b>, <b>Review Analysis</b></li>
                    <li>Set the <b>Low Stock Alert Threshold</b> for smart alerts</li>
                </ol>
            </div>

        </div>
        """
