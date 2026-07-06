/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, useState, onWillStart, onMounted, useRef } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { loadJS } from "@web/core/assets";
import { rpc } from "@web/core/network/rpc";

class AmazonDashboard extends Component {
    static template = "sdlc_amazon_connector.Dashboard";

    setup() {
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({
            kpis: {},
            charts: {},
            recentSyncs: [],
            instances: [],
            selectedInstance: null,
            dateRange: "30",
            customFrom: "",
            customTo: "",
            aiInsights: "Click 'AI Insights' to generate...",
            loading: true,
            aiLoading: false,
            optimizing: false,
        });
        this.salesChartRef = useRef("salesChart");
        this.statusChartRef = useRef("statusChart");
        this.productsChartRef = useRef("productsChart");
        this.revenueChartRef = useRef("revenueChart");
        this._chartInstances = {};

        onWillStart(async () => {
            await loadJS("https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js");
            await this.loadInstances();
            await this.loadData();
        });

        onMounted(() => this.renderCharts());
    }

    async loadInstances() {
        try {
            const data = await rpc("/amazon/dashboard/instances", {});
            this.state.instances = data.instances || [];
        } catch (e) {
            console.error("Failed to load instances:", e);
        }
    }

    async loadData() {
        this.state.loading = true;
        try {
            const params = {
                instance_id: this.state.selectedInstance || null,
                date_range: this.state.dateRange,
                custom_from: this.state.customFrom || null,
                custom_to: this.state.customTo || null,
            };
            const data = await rpc("/amazon/dashboard/data", params);
            this.state.kpis = data.kpis || {};
            this.state.charts = data.charts || {};
            this.state.recentSyncs = data.recent_syncs || [];
            this.state.loading = false;
        } catch (e) {
            console.error("Dashboard load error:", e);
            this.state.loading = false;
        }
    }

    async onInstanceChange(ev) {
        const val = ev.target.value;
        this.state.selectedInstance = val === "all" ? null : parseInt(val);
        await this.loadData();
        this.renderCharts();
    }

    async onDateRangeChange(ev) {
        this.state.dateRange = ev.target.value;
        if (this.state.dateRange !== "custom") {
            await this.loadData();
            this.renderCharts();
        }
    }

    async onApplyCustomDate() {
        if (this.state.customFrom && this.state.customTo) {
            await this.loadData();
            this.renderCharts();
        }
    }

    _destroyCharts() {
        for (const key of Object.keys(this._chartInstances)) {
            if (this._chartInstances[key]) {
                this._chartInstances[key].destroy();
                this._chartInstances[key] = null;
            }
        }
    }

    renderCharts() {
        if (!window.Chart) return;
        setTimeout(() => {
            this._destroyCharts();
            this._renderSalesChart();
            this._renderStatusChart();
            this._renderProductsChart();
            this._renderRevenueChart();
        }, 150);
    }

    _renderSalesChart() {
        const el = this.salesChartRef.el;
        if (!el) return;
        const data = this.state.charts.daily_sales || [];
        this._chartInstances.sales = new Chart(el, {
            type: "line",
            data: {
                labels: data.map(d => d.date.slice(5)),
                datasets: [{
                    label: "Orders",
                    data: data.map(d => d.orders),
                    borderColor: "#667eea",
                    backgroundColor: "rgba(102,126,234,0.1)",
                    fill: true, tension: 0.4,
                }, {
                    label: "Revenue",
                    data: data.map(d => d.revenue),
                    borderColor: "#11998e",
                    backgroundColor: "rgba(17,153,142,0.05)",
                    fill: true, tension: 0.4, yAxisID: "y1",
                }],
            },
            options: {
                responsive: true,
                scales: {
                    y: { position: "left", title: { display: true, text: "Orders" } },
                    y1: { position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "Revenue" } },
                },
            },
        });
    }

    _renderStatusChart() {
        const el = this.statusChartRef.el;
        if (!el) return;
        const data = this.state.charts.status_distribution || {};
        const colors = { Pending: "#f39c12", Unshipped: "#e67e22", Shipped: "#27ae60", Canceled: "#e74c3c", Unfulfillable: "#95a5a6" };
        this._chartInstances.status = new Chart(el, {
            type: "doughnut",
            data: {
                labels: Object.keys(data),
                datasets: [{ data: Object.values(data), backgroundColor: Object.keys(data).map(k => colors[k] || "#667eea") }],
            },
            options: { responsive: true },
        });
    }

    _renderProductsChart() {
        const el = this.productsChartRef.el;
        if (!el) return;
        const data = this.state.charts.top_products || [];
        this._chartInstances.products = new Chart(el, {
            type: "bar",
            data: {
                labels: data.map(d => d.name),
                datasets: [{ label: "Units Sold", data: data.map(d => d.qty), backgroundColor: "#ff9900" }],
            },
            options: { responsive: true, indexAxis: "y" },
        });
    }

    _renderRevenueChart() {
        const el = this.revenueChartRef.el;
        if (!el) return;
        const data = this.state.charts.weekly_revenue || [];
        this._chartInstances.revenue = new Chart(el, {
            type: "bar",
            data: {
                labels: data.map(d => d.week),
                datasets: [
                    { label: "Revenue", data: data.map(d => d.revenue), backgroundColor: "#11998e", yAxisID: "y" },
                    { label: "Orders", data: data.map(d => d.orders), backgroundColor: "#667eea", type: "line", yAxisID: "y1" },
                ],
            },
            options: {
                responsive: true,
                scales: { y: { position: "left" }, y1: { position: "right", grid: { drawOnChartArea: false } } },
            },
        });
    }

    async getAIInsights() {
        this.state.aiLoading = true;
        try {
            const data = await rpc("/amazon/dashboard/ai-insights", { instance_id: this.state.selectedInstance });
            this.state.aiInsights = data.insights || "No insights.";
        } catch (e) { this.state.aiInsights = "Error: " + e.message; }
        this.state.aiLoading = false;
    }

    async optimizeStore() {
        this.state.optimizing = true;
        try {
            const data = await rpc("/amazon/dashboard/optimize-store", { instance_id: this.state.selectedInstance });
            this.notification.add(data.message || "Done!", { type: "success", sticky: true });
            this.state.aiInsights = "Results:\n" + (data.actions || []).join("\n");
            await this.loadData();
            this.renderCharts();
        } catch (e) { this.notification.add("Failed: " + e.message, { type: "danger" }); }
        this.state.optimizing = false;
    }

    openOrders() { this.action.doAction("sdlc_amazon_connector.amazon_sale_order_action"); }
    openProducts() { this.action.doAction("sdlc_amazon_connector.amazon_product_action"); }
    openAlerts() { this.action.doAction("sdlc_amazon_connector.amazon_alert_action"); }
    openDelivery() { this.action.doAction("sdlc_amazon_connector.amazon_delivery_all_action"); }
    openSyncLogs() { this.action.doAction("sdlc_amazon_connector.amazon_sync_report_action"); }

    formatCurrency(val) {
        return "\u20B9" + (val || 0).toLocaleString("en-IN", { maximumFractionDigits: 0 });
    }
}

registry.category("actions").add("amazon_dashboard", AmazonDashboard);
