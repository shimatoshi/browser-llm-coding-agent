// QuickPulse App JavaScript
const WEATHER_API = 'https://api.open-meteo.com/v1/forecast';
const HN_API = 'https://hacker-news.firebaseio.com/v0';

const state = {
    isLoading: false,
    news: [],
    weather: null,
    lastUpdated: null,
    deferredPrompt: null
};

// DOM Elements
const fetchBtn = document.getElementById('fetchBtn');
const statusBar = document.getElementById('statusBar');
const statusText = document.getElementById('statusText');
const newsContainer = document.getElementById('newsContainer');
const weatherContainer = document.getElementById('weatherContainer');
const newsEmpty = document.getElementById('newsEmpty');
const weatherEmpty = document.getElementById('weatherEmpty');
const lastUpdated = document.getElementById('lastUpdated');
const installPrompt = document.getElementById('installPrompt');
const installBtn = document.getElementById('installBtn');
const dismissBtn = document.getElementById('dismissBtn');

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    loadCachedData();
    setupInstallPrompt();
    checkOnlineStatus();
    
    // Auto-refresh every 30 minutes
    setInterval(() => {
        if (!state.isLoading && navigator.onLine) {
            fetchAll();
        }
    }, 30 * 60 * 1000);
});

// Event Listeners
fetchBtn.addEventListener('click', () => {
    if (!state.isLoading) {
        fetchAll();
    }
});

window.addEventListener('online', checkOnlineStatus);
window.addEventListener('offline', checkOnlineStatus);

// Network Status
function checkOnlineStatus() {
    if (navigator.onLine) {
        statusBar.classList.remove('offline');
        statusText.textContent = state.lastUpdated 
            ? `${formatTime(state.lastUpdated)}に更新済み` 
            : 'ニュースと天気を取得するにはタップ';
    } else {
        statusBar.classList.add('offline');
        statusText.textContent = 'オフラインです (キャッシュを表示中)';
    }
}

// Main Fetch Function
async function fetchAll() {
    if (state.isLoading) return;
    
    state.isLoading = true;
    updateButtonState(true);
    showSkeletons();
    statusText.textContent = 'データを取得中...';
    
    try {
        const [newsData, weatherData] = await Promise.all([
            fetchNews(),
            fetchWeather()
        ]);
        
        state.news = newsData;
        state.weather = weatherData;
        state.lastUpdated = new Date();
        
        cacheData();
        renderNews();
        renderWeather();
        
        statusText.textContent = `${formatTime(state.lastUpdated)}に更新済み`;
        statusBar.classList.remove('offline');
        
    } catch (error) {
        console.error('Fetch error:', error);
        statusText.textContent = '取得に失敗しました。再度タップしてください';
        
        // Try to load cached data
        loadCachedData();
    } finally {
        state.isLoading = false;
        updateButtonState(false);
    }
}

// News API - Hacker News (no API key needed)
async function fetchNews() {
    try {
        // Fetch top stories from Hacker News
        const response = await fetch(`${HN_API}/topstories.json`);
        const storyIds = await response.json();
        
        // Get top 10 stories
        const topIds = storyIds.slice(0, 10);
        const stories = await Promise.all(
            topIds.map(id => fetch(`${HN_API}/item/${id}.json`).then(r => r.json()))
        );
        
        return stories.filter(s => s && s.title).map(story => ({
            title: story.title,
            link: story.url || `https://news.ycombinator.com/item?id=${story.id}`,
            source_id: 'Hacker News',
            pubDate: new Date(story.time * 1000).toISOString(),
            score: story.score
        }));
    } catch (error) {
        console.error('News fetch error:', error);
        return [];
    }
}

// Weather API
async function fetchWeather() {
    // Tokyo coordinates
    const params = new URLSearchParams({
        latitude: 35.6762,
        longitude: 139.6503,
        daily: 'weather_code,temperature_2m_max,temperature_2m_min',
        timezone: 'Asia/Tokyo',
        forecast_days: 5
    });
    
    const response = await fetch(`${WEATHER_API}?${params}`);
    if (!response.ok) throw new Error('Weather fetch failed');
    
    return await response.json();
}

// Render News
function renderNews() {
    newsContainer.innerHTML = '';
    
    if (!state.news || state.news.length === 0) {
        newsContainer.innerHTML = `
            <div class="empty-state">
                <div class="icon">📰</div>
                <p>ニュースが見つかりませんでした</p>
            </div>
        `;
        return;
    }
    
    state.news.forEach((article, index) => {
        const card = document.createElement('a');
        card.href = article.link || '#';
        card.target = '_blank';
        card.rel = 'noopener noreferrer';
        card.className = 'card news-card fade-in';
        card.style.animationDelay = `${index * 50}ms`;
        
        const pubDate = article.pubDate ? new Date(article.pubDate) : null;
        
        card.innerHTML = `
            <h3>${escapeHtml(article.title || 'タイトルなし')}</h3>
            <div class="meta">
                <span class="source">${escapeHtml(article.source_id || article.source_name || '不明')}</span>
                <span>${pubDate ? formatDate(pubDate) : '--'}</span>
            </div>
        `;
        
        newsContainer.appendChild(card);
    });
}

// Render Weather
function renderWeather() {
    if (!state.weather || !state.weather.daily) {
        weatherContainer.innerHTML = `
            <div class="empty-state">
                <div class="icon">🌤️</div>
                <p>天気情報が見つかりませんでした</p>
            </div>
        `;
        return;
    }
    
    const { daily } = state.weather;
    const weatherIcons = getWeatherIcons(daily.weather_code);
    
    let html = `
        <div class="card current-weather fade-in">
            <div class="condition">東京都 - 今日の天気</div>
            <div class="main">
                <span class="weather-icon">${weatherIcons[0]}</span>
                <div>
                    <span class="temp">${Math.round(daily.temperature_2m_max[0])}°</span>
                </div>
            </div>
        </div>
        <div class="forecast-grid">
    `;
    
    for (let i = 0; i < 5; i++) {
        const date = new Date(daily.time[i]);
        html += `
            <div class="forecast-card fade-in" style="animation-delay: ${(i + 1) * 100}ms">
                <div class="day">${formatDay(date)}</div>
                <div class="icon">${weatherIcons[i]}</div>
                <div class="temp">
                    <span class="high">${Math.round(daily.temperature_2m_max[i])}°</span>
                    <span class="low">${Math.round(daily.temperature_2m_min[i])}°</span>
                </div>
            </div>
        `;
    }
    
    html += '</div>';
    weatherContainer.innerHTML = html;
}

// Show Skeleton Loaders
function showSkeletons() {
    // News skeletons
    let newsSkeleton = '<div class="empty-state hidden" id="newsEmpty"></div>';
    for (let i = 0; i < 6; i++) {
        newsSkeleton += `
            <div class="card skeleton-card">
                <div class="skeleton skeleton-title"></div>
                <div class="skeleton skeleton-text"></div>
                <div class="skeleton skeleton-text" style="width: 50%"></div>
                <div class="skeleton skeleton-meta"></div>
            </div>
        `;
    }
    newsContainer.innerHTML = newsSkeleton;
    
    // Weather skeletons
    let weatherSkeleton = '<div class="empty-state hidden" id="weatherEmpty"></div>';
    weatherSkeleton += `
        <div class="card current-weather">
            <div class="skeleton" style="height: 80px; width: 100%"></div>
        </div>
        <div class="forecast-grid">
    `;
    for (let i = 0; i < 5; i++) {
        weatherSkeleton += `
            <div class="forecast-card">
                <div class="skeleton" style="height: 16px; width: 100%"></div>
                <div class="skeleton" style="height: 40px; width: 100%; margin: 12px 0"></div>
                <div class="skeleton" style="height: 20px; width: 100%"></div>
            </div>
        `;
    }
    weatherSkeleton += '</div>';
    weatherContainer.innerHTML = weatherSkeleton;
}

// Update Button State
function updateButtonState(loading) {
    if (loading) {
        fetchBtn.disabled = true;
        fetchBtn.classList.add('loading');
        fetchBtn.innerHTML = `
            <div class="spinner"></div>
            <span>取得中</span>
        `;
    } else {
        fetchBtn.disabled = false;
        fetchBtn.classList.remove('loading');
        fetchBtn.innerHTML = `
            <span class="icon">⚡</span>
            <span>まとめて取得</span>
        `;
    }
}

// Cache Management
function cacheData() {
    const cacheData = {
        news: state.news,
        weather: state.weather,
        lastUpdated: state.lastUpdated.toISOString()
    };
    localStorage.setItem('quickpulse_data', JSON.stringify(cacheData));
}

function loadCachedData() {
    try {
        const cached = localStorage.getItem('quickpulse_data');
        if (cached) {
            const data = JSON.parse(cached);
            state.news = data.news || [];
            state.weather = data.weather || null;
            state.lastUpdated = data.lastUpdated ? new Date(data.lastUpdated) : null;
            
            if (state.news.length > 0) renderNews();
            if (state.weather) renderWeather();
            
            if (state.lastUpdated) {
                lastUpdated.textContent = `最終更新: ${formatTime(state.lastUpdated)}`;
            }
        }
    } catch (e) {
        console.error('Cache load error:', e);
    }
}

// PWA Install Prompt
function setupInstallPrompt() {
    window.addEventListener('beforeinstallprompt', (e) => {
        e.preventDefault();
        state.deferredPrompt = e;
        
        // Show install prompt after 5 seconds
        setTimeout(() => {
            if (!localStorage.getItem('install_dismissed')) {
                installPrompt.classList.add('show');
            }
        }, 5000);
    });
    
    installBtn.addEventListener('click', async () => {
        if (state.deferredPrompt) {
            state.deferredPrompt.prompt();
            const { outcome } = await state.deferredPrompt.userChoice;
            if (outcome === 'accepted') {
                installPrompt.classList.remove('show');
            }
            state.deferredPrompt = null;
        }
    });
    
    dismissBtn.addEventListener('click', () => {
        installPrompt.classList.remove('show');
        localStorage.setItem('install_dismissed', 'true');
    });
}

// Weather Code to Icons
function getWeatherIcons(codes) {
    if (!Array.isArray(codes)) return ['☀️'];
    
    return codes.map(code => {
        if (code === 0) return '☀️';
        if (code <= 3) return '⛅';
        if ([45, 48].includes(code)) return '🌫️';
        if ([51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82].includes(code)) return '🌧️';
        if ([71, 73, 75, 77, 85, 86].includes(code)) return '❄️';
        if ([95, 96, 99].includes(code)) return '⛈️';
        return '🌤️';
    });
}

// Utility Functions
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatTime(date) {
    if (!date) return '--';
    const d = date instanceof Date ? date : new Date(date);
    return d.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
}

function formatDate(date) {
    const d = date instanceof Date ? date : new Date(date);
    const now = new Date();
    const diff = now - d;
    
    if (diff < 86400000) return '今日';
    if (diff < 172800000) return '昨日';
    if (diff < 604800000) return `${Math.floor(diff / 86400000)}日前`;
    
    return d.toLocaleDateString('ja-JP', { month: 'short', day: 'numeric' });
}

function formatDay(date) {
    const days = ['日', '月', '火', '水', '木', '金', '土'];
    return `${date.getMonth() + 1}/${date.getDate()} (${days[date.getDay()]})`;
}

// Service Worker Registration
if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        navigator.serviceWorker.register('sw.js')
            .then(reg => console.log('SW registered'))
            .catch(err => console.log('SW registration failed:', err));
    });
}