// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Connect Health EHR Interface Configuration
 * 
 * Update these values when deploying to different environments.
 * See DEPLOYMENT_GUIDE.md Step 3b for instructions.
 */

(function() {
    'use strict';
    
    // Detect environment based on hostname
    const hostname = window.location.hostname;
    
    // ==========================================================================
    // DEMO MODE
    // ==========================================================================
    // Demo mode. On localhost there is usually no AWS account wired up, so
    // default it ON: the README advertises a no-AWS local demo and without
    // this the schedule renders empty on first run (the live HealthLake call
    // fails). An explicit choice stored in localStorage always wins, so
    // toggling it off on localhost is respected.
    var _demoPref = localStorage.getItem('demoMode');
    // Hosts that mean "someone is running this locally". A colleague opening
    // the demo by LAN IP or machine.local is still local, and must still get
    // demo mode, otherwise the schedule renders empty against live AWS.
    var _isLocal = ['localhost', '127.0.0.1', '::1', '0.0.0.0', ''].indexOf(hostname) !== -1
        || /\.local$/i.test(hostname)
        || /^10\./.test(hostname)
        || /^192\.168\./.test(hostname)
        || /^172\.(1[6-9]|2[0-9]|3[01])\./.test(hostname);
    window.DEMO_MODE = _demoPref === null ? _isLocal : _demoPref === 'true';
    
    window.demoHeaders = function(extra) {
        const h = extra ? Object.assign({}, extra) : {};
        if (window.DEMO_MODE) h['X-Demo-Mode'] = 'true';
        return h;
    };
    
    window.toggleDemoMode = function() {
        window.DEMO_MODE = !window.DEMO_MODE;
        localStorage.setItem('demoMode', window.DEMO_MODE);
        _updateDemoBadge();
        console.log('[Demo] Mode:', window.DEMO_MODE ? 'ON' : 'OFF');
        // The patient list and other page data are fetched once on page load,
        // so the X-Demo-Mode header only affects LATER requests. Without a
        // reload the schedule stays empty after enabling demo mode (the initial
        // live call already failed). Reload so everything re-fetches in the
        // newly selected mode.
        window.location.reload();
    };
    
    function _updateDemoBadge() {
        let badge = document.getElementById('demoBadge');
        if (window.DEMO_MODE) {
            if (!badge) {
                badge = document.createElement('div');
                badge.id = 'demoBadge';
                badge.style.cssText = 'position:fixed;top:8px;left:50%;transform:translateX(-50%);z-index:99999;' +
                    'background:#f59e0b;color:#000;padding:3px 12px;border-radius:12px;font-size:11px;' +
                    'font-weight:600;letter-spacing:0.5px;cursor:pointer;user-select:none;opacity:0.9;' +
                    'font-family:-apple-system,BlinkMacSystemFont,sans-serif;box-shadow:0 1px 4px rgba(0,0,0,0.2);';
                badge.textContent = 'DEMO MODE';
                badge.title = 'Click or Ctrl+Shift+D to toggle off';
                badge.onclick = window.toggleDemoMode;
                document.body.appendChild(badge);
            }
            badge.style.display = 'block';
        } else if (badge) {
            badge.style.display = 'none';
        }
    }
    
    document.addEventListener('keydown', function(e) {
        if (e.ctrlKey && e.shiftKey && e.key === 'D') {
            e.preventDefault();
            window.toggleDemoMode();
        }
    });
    
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _updateDemoBadge);
    } else {
        _updateDemoBadge();
    }
    
    // ==========================================================================
    // COGNITO AUTHENTICATION
    // ==========================================================================
    // Set these after deploying the cognito-stack.yaml CloudFormation template.
    // Leave empty to disable authentication (local development default).
    window.COGNITO_CONFIG = {
        userPoolId: 'YOUR_COGNITO_USER_POOL_ID',
        clientId: 'YOUR_COGNITO_CLIENT_ID',
        region: 'us-east-1'
    };

    // ==========================================================================
    // CLINIC CONTACT NUMBERS
    // ==========================================================================
    // Replace these with your actual clinic phone numbers.
    // SCHEDULING_PHONE appears in the SMS follow-up message template.
    // OFFICE_PHONE appears in the UI footer and SMS message.
    window.CLINIC_PHONE = {
        schedulingNumber: '(555) 123-4567',   // toll-free or scheduling line
        officeNumber: '(555) 123-4567'        // front desk / office line
    };

    // Environment configurations
    // Update the 'deployed' block with your CloudFront distribution URLs after deployment.
    const configs = {
        local: {
            WS_URL: 'ws://localhost:8081/stream',
            BACKEND_URL: 'http://localhost:5000',
            ENV_NAME: 'local'
        },
        deployed: {
            WS_URL: 'wss://ch-bridge-dev-421355715.us-east-1.elb.amazonaws.com/stream',
            BACKEND_URL: 'https://d1exampleabcdef.cloudfront.net',
            ENV_NAME: 'deployed'
        }
    };
    
    let activeConfig;
    if (_isLocal) {
        activeConfig = configs.local;
    } else {
        activeConfig = configs.deployed;
    }

    window.WS_URL = activeConfig.WS_URL;
    // The backend serves this page in BOTH setups (Flask locally, the same
    // container behind CloudFront when deployed), so the API is always on the
    // page's own origin. Deriving it avoids two failure modes that produced a
    // fully-rendered dashboard with zero patients:
    //   - opening the app by LAN IP, machine.local or [::1] fell through to the
    //     `deployed` branch and sent /api/* to a placeholder CloudFront domain
    //   - opening it at 127.0.0.1 sent /api/* to localhost, a different origin,
    //     making every call depend on CORS
    // Set window.BACKEND_URL before this script to override.
    window.BACKEND_URL = window.BACKEND_URL || window.location.origin;
    window.ENV_NAME = activeConfig.ENV_NAME;
    
    console.log('[Config] Environment:', activeConfig.ENV_NAME);
    console.log('[Config] WebSocket URL:', activeConfig.WS_URL);
    console.log('[Config] Backend URL:', activeConfig.BACKEND_URL);
})();
