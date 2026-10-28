# Payment Microservice: Frontend Developer Integration Guide

This guide provides frontend engineers (React, Next.js, Vue, mobile apps, and vanilla JavaScript) with everything needed to integrate with the Payment Microservice.

---

## 1. Important Security Boundaries

The Payment Microservice enforces a strict zero-trust security perimeter:

1. **Service-to-Service API Boundary**:
   - The Payment Microservice API (`/api/v1/payments/init`, `/api/v1/payments/{paymentId}`) requires an `X-API-Key`.
   - **A frontend browser or mobile app MUST NEVER possess or send the `X-API-Key`**, Razorpay `key_secret`, or webhook signing secrets.
   - The frontend calls its own backend (e.g., `community-backend`), and the backend calls the Payment Microservice using its authenticated service credentials.

2. **Publicly Accessible Endpoints**:
   - The `/checkout/**` endpoints are public and secured with rate-limiting, clickjacking protection, and strict Content Security Policies (CSP).
   - `GET /checkout/{paymentId}`: Standalone responsive hosted checkout page.
   - `GET /checkout/api/details/{paymentId}`: Order details (contains only public keys, **never secrets**).
   - `POST /checkout/api/verify`: Cryptographic signature verification for payments completed via Razorpay.

---

## 2. Environments and Base URLs

| Environment | Base URL | Protocol | Notes |
|---|---|---|---|
| **Local Development** | `http://localhost:8102` | HTTP | Local Docker / Spring Boot |
| **Testing Dashboard** | `http://localhost:5055` | HTTP | Mock & sandbox testing dashboard |
| **Production Server** | `http://handorg.duckdns.org` | HTTP | Reverse-proxied via Host Nginx |

> [!NOTE]
> All checkout and verification requests use standard HTTP headers (`Host`, `X-Forwarded-For`, `X-Forwarded-Proto`) managed transparently by Nginx. Relative paths (e.g., `/checkout/api/...`) should always be used in browser JavaScript.

---

## 3. High-Level Integration Flows

Frontend developers have two main choices for payment integration:

```mermaid
flowchart TD
    subgraph Flow A: Hosted Checkout Redirect (Recommended)
        A1[1. User clicks Pay in Frontend] --> A2[2. Frontend calls Backend API]
        A2 --> A3[3. Backend calls Payment MS init]
        A3 --> A4[4. Backend returns payment_page_url]
        A4 --> A5[5. Frontend redirects to payment_page_url]
        A5 --> A6[6. User pays on Hosted Checkout Page]
        A6 --> A7[7. Checkout verifies signature & redirects to callback_url]
    end

    subgraph Flow B: Embedded Razorpay Modal (Custom UI)
        B1[1. User clicks Pay in Frontend] --> B2[2. Frontend calls Backend API]
        B2 --> B3[3. Backend calls Payment MS init]
        B3 --> B4[4. Backend returns payment_id & order_id]
        B4 --> B5[5. Frontend fetches /checkout/api/details/payment_id]
        B5 --> B6[6. Frontend opens Razorpay Checkout modal]
        B6 --> B7[7. On success, Frontend POSTs to /checkout/api/verify]
        B7 --> B8[8. Microservice confirms signature & completes order]
    end
```

---

## 4. TypeScript Interfaces & Data Contracts

### 4.1 Payment Initiation (Backend to Payment Microservice)

```typescript
export interface CustomerDetails {
  name?: string;
  email?: string;
  phone?: string; // 10-digit or E.164 format
}

export interface PaymentInitRequest {
  amount: number | string;            // Either units (e.g. 199.50) or string
  amountInPaise?: number;             // Optional explicit paise (e.g. 19950)
  tenant: string;                     // Tenant ID (e.g. "tenant-1" or "tenant-2")
  currency?: string;                  // Default: "INR"
  customer_details?: CustomerDetails; // Pre-fills Razorpay checkout modal
  description?: string;               // Displayed on checkout screen
  callback_url?: string;              // Return URL after payment completes
  idempotencyKey?: string;            // Prevents duplicate orders on network retries
  metadate?: Record<string, unknown>; // Custom tenant application metadata
}

export interface PaymentInitResponse {
  payment_id: string;        // Internal Payment ID: "pay_..."
  order_id: string;          // Razorpay Order ID: "order_..."
  payment_page_url: string;  // Fully qualified hosted checkout URL
  status: 'INITIATED' | 'COMPLETED' | 'FAILED' | 'EXPIRED';
  amount: number;            // Amount in paise (e.g. 19900 for ₹199.00)
  currency: string;          // "INR"
  tenant_id: string;         // Active tenant identifier
}
```

### 4.2 Checkout Details (Public Endpoint: `GET /checkout/api/details/{paymentId}`)

```typescript
export interface CheckoutDetailsResponse {
  paymentId: string;
  orderId: string;
  tenantId: string;
  tenantName: string;
  amount: number;             // In paise (10000 = ₹100.00)
  amountFormatted: string;    // Formatted decimal ("100.00")
  currency: string;           // "INR"
  description: string;
  customerName?: string;
  customerEmail?: string;
  customerPhone?: string;
  razorpayKeyId: string;      // Public Key ID (e.g. "rzp_test_...") - Safe for browser
  isCompleted: boolean;
  callbackUrl?: string;
}
```

### 4.3 Signature Verification (Public Endpoint: `POST /checkout/api/verify`)

```typescript
export interface VerifyPaymentRequest {
  paymentId: string;
  razorpayOrderId: string;
  razorpayPaymentId: string;
  razorpaySignature: string;
}

export interface VerifyPaymentResponse {
  verified: boolean;
  paymentId: string;
  razorpayOrderId: string;
  razorpayPaymentId: string;
  status: 'COMPLETED' | 'FAILED';
  message: string;
  callbackUrl?: string;
}
```

---

## 5. Flow A: Hosted Checkout Redirect (Recommended)

This is the fastest, cleanest, and most secure method. The user is redirected to a pre-built, responsive, high-converting checkout page hosted by the Payment Microservice.

### How it Works:
1. Frontend makes an API request to the application backend: `POST /api/subscription/create` or `POST /api/order/checkout`.
2. Backend validates the cart/order, calls Payment Microservice `POST /api/v1/payments/init`, and receives `payment_page_url`.
3. Frontend redirects the browser: `window.location.href = response.payment_page_url`.
4. The user completes payment on the hosted page.
5. Upon successful payment verification, the hosted page redirects back to your `callback_url` with query parameters:
   `http://yourapp.com/payment/return?status=COMPLETED&payment_id=pay_123&order_id=order_456`

### React / Next.js Implementation Example:

```tsx
import React, { useState } from 'react';

export const CheckoutButton: React.FC<{ planId: string }> = ({ planId }) => {
  const [loading, setLoading] = useState(false);

  const handleCheckout = async () => {
    try {
      setLoading(true);

      // Call your existing Backend API (NOT the Payment Microservice directly)
      const res = await fetch('/api/v1/orders/initiate-payment', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${userToken}`,
        },
        body: JSON.stringify({
          planId,
          callbackUrl: `${window.location.origin}/payment/return`,
        }),
      });

      if (!res.ok) {
        throw new Error('Failed to initiate payment session');
      }

      const { payment_page_url } = await res.json();

      // Redirect user to the hosted checkout page
      window.location.href = payment_page_url;
    } catch (error) {
      console.error('Checkout error:', error);
      alert('Unable to proceed to payment. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <button 
      onClick={handleCheckout} 
      disabled={loading}
      className="btn-pay"
    >
      {loading ? 'Securing Session...' : 'Subscribe Now'}
    </button>
  );
};
```

---

## 6. Flow B: Embedded Razorpay Modal (Custom In-App UI)

If your product requires an in-app checkout experience without full-page redirection, you can load Razorpay's checkout script directly inside your web application.

### Step 1: Include Razorpay Checkout Script
Add Razorpay Checkout SDK to your `index.html` or load it dynamically:
```html
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
```

Or load dynamically in React:
```typescript
export function loadRazorpayScript(): Promise<boolean> {
  return new Promise((resolve) => {
    if (window.Razorpay) {
      resolve(true);
      return;
    }
    const script = document.createElement('script');
    script.src = 'https://checkout.razorpay.com/v1/checkout.js';
    script.onload = () => resolve(true);
    script.onerror = () => resolve(false);
    document.body.appendChild(script);
  });
}
```

### Step 2: Full React Component Implementation

```tsx
import React, { useState } from 'react';
import { loadRazorpayScript } from './utils/razorpay';

declare global {
  interface Window {
    Razorpay: any;
  }
}

export const InAppPaymentComponent: React.FC = () => {
  const [processing, setProcessing] = useState(false);

  const startPayment = async () => {
    setProcessing(true);

    try {
      // 1. Ensure Razorpay SDK is loaded
      const isLoaded = await loadRazorpayScript();
      if (!isLoaded) {
        throw new Error('Razorpay SDK failed to load. Are you connected to the internet?');
      }

      // 2. Call your backend to get paymentId & orderId
      const initRes = await fetch('/api/v1/orders/create-payment', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount: 299, plan: 'Gold' })
      });
      const { payment_id } = await initRes.json();

      // 3. Fetch checkout details (Public Key, Amount, Currency) from Payment Microservice
      const detailsRes = await fetch(`http://handorg.duckdns.org/checkout/api/details/${payment_id}`);
      if (!detailsRes.ok) {
        throw new Error('Failed to fetch payment session details');
      }
      const details = await detailsRes.json();

      // 4. Configure and open Razorpay Modal
      const options = {
        key: details.razorpayKeyId, // Public Key: Safe for browser
        amount: details.amount,     // In paise
        currency: details.currency,
        name: details.tenantName,
        description: details.description,
        order_id: details.orderId,
        prefill: {
          name: details.customerName || '',
          email: details.customerEmail || '',
          contact: details.customerPhone || ''
        },
        theme: {
          color: '#D9A441' // Match OTT brand gold
        },
        modal: {
          ondismiss: () => {
            setProcessing(false);
            console.log('Checkout modal was closed by the user.');
          }
        },
        handler: async (response: {
          razorpay_payment_id: string;
          razorpay_order_id: string;
          razorpay_signature: string;
        }) => {
          // 5. Send signature for cryptographic server verification
          try {
            const verifyRes = await fetch('http://handorg.duckdns.org/checkout/api/verify', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                paymentId: payment_id,
                razorpayOrderId: response.razorpay_order_id,
                razorpayPaymentId: response.razorpay_payment_id,
                razorpaySignature: response.razorpay_signature
              })
            });

            const verifyResult = await verifyRes.json();

            if (verifyResult.verified) {
              window.location.href = `/payment/success?paymentId=${payment_id}`;
            } else {
              window.location.href = `/payment/failed?error=${encodeURIComponent(verifyResult.message)}`;
            }
          } catch (err) {
            console.error('Signature verification call failed:', err);
            window.location.href = `/payment/status?paymentId=${payment_id}`;
          }
        }
      };

      const rzp = new window.Razorpay(options);
      rzp.on('payment.failed', (failedResponse: any) => {
        console.error('Payment failed at gateway:', failedResponse.error);
        alert(`Payment Failed: ${failedResponse.error.description}`);
        setProcessing(false);
      });

      rzp.open();
    } catch (error: any) {
      alert(error.message || 'Payment initiation failed.');
      setProcessing(false);
    }
  };

  return (
    <button onClick={startPayment} disabled={processing} className="payment-btn">
      {processing ? 'Processing...' : 'Pay with Razorpay'}
    </button>
  );
};
```

---

## 7. Return URL & Callback Handling

When using Hosted Checkout, after completion the browser is redirected to the `callback_url` specified during initialization:

```text
http://app.example.com/payment/return?status=COMPLETED&payment_id=pay_748cedad23664beaa53a94a030aca260&order_id=order_TsUQIlgzAP03ix
```

### Query Parameters:
| Parameter | Type | Description |
|---|---|---|
| `status` | `string` | `COMPLETED` on success, `FAILED` if payment failed. |
| `payment_id` | `string` | The internal tracking payment ID (`pay_...`). |
| `order_id` | `string` | Razorpay order ID (`order_...`). |

### Recommended Return Page Pattern:
Even though the frontend receives `status=COMPLETED`, **the frontend should poll or query the backend** to ensure the backend has processed the transactional outbox webhook event and activated the user's entitlements.

```tsx
// pages/payment/return.tsx
import React, { useEffect, useState } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';

export const PaymentReturnPage: React.FC = () => {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [status, setStatus] = useState<'checking' | 'confirmed' | 'failed'>('checking');

  const paymentId = searchParams.get('payment_id');
  const urlStatus = searchParams.get('status');

  useEffect(() => {
    if (urlStatus === 'FAILED') {
      setStatus('failed');
      return;
    }

    // Verify state with backend to ensure database webhook sync completed
    let attempts = 0;
    const interval = setInterval(async () => {
      attempts++;
      try {
        const res = await fetch(`/api/v1/orders/status/${paymentId}`);
        const data = await res.json();

        if (data.status === 'COMPLETED') {
          clearInterval(interval);
          setStatus('confirmed');
          setTimeout(() => navigate('/dashboard'), 3000);
        } else if (attempts > 5) {
          clearInterval(interval);
          setStatus('confirmed'); // Fallback after 10s
        }
      } catch (e) {
        if (attempts > 5) clearInterval(interval);
      }
    }, 2000);

    return () => clearInterval(interval);
  }, [paymentId, urlStatus, navigate]);

  return (
    <div className="status-screen">
      {status === 'checking' && <h2>Confirming your payment...</h2>}
      {status === 'confirmed' && <h2>Payment Successful! Redirecting to dashboard...</h2>}
      {status === 'failed' && <h2>Payment was not completed. Please try again.</h2>}
    </div>
  );
};
```

---

## 8. HTTP Status Codes & Error Handling

The Payment Microservice uses standard HTTP response status codes:

| HTTP Status | Error / Scenario | Recommended Frontend Action |
|---|---|---|
| `200 OK` | Details fetched or signature verified successfully. | Proceed with flow. |
| `400 Bad Request` | Missing required fields, expired order, or malformed data. | Display user-friendly error message. |
| `403 Forbidden` | Signature mismatch or unauthorized tenant access. | Display "Payment could not be verified". |
| `404 Not Found` | Unknown `paymentId`. | Display "Payment session expired or not found". |
| `429 Too Many Requests` | Rate limit exceeded (DDoS & Brute force mitigation). | Show "Too many requests. Please wait 60 seconds." |
| `500 Internal Error` | Gateway communication or database failure. | Show generic error with option to retry. |

### Rate Limiting Protection Enforced:
- `/checkout/api/verify`: **20 requests per minute** per IP.
- `/checkout/**` (Details/HTML): **60 requests per minute** per IP.
- `/api/v1/payments/**` (Internal API): **120 requests per minute** per client.

If a rate limit is exceeded, the server returns HTTP `429` with header `Retry-After: 60`.

---

## 9. Mobile App Considerations (React Native, Flutter, iOS, Android)

1. **WebView Support**:
   - When loading `payment_page_url` inside a React Native or Flutter `WebView`, ensure:
     - `javaScriptEnabled={true}`
     - `domStorageEnabled={true}`
     - Listen to URL navigation events (`onNavigationStateChange`) to intercept `callback_url` when payment completes.

2. **Deep Linking Callback**:
   - You can configure the `callback_url` to be a mobile deep link:
     ```json
     {
       "callback_url": "communityott://payment/return"
     }
     ```
   - When the user finishes payment on the mobile browser, the hosted checkout automatically launches your app via the custom URL scheme.

3. **Pop-up Blocker Compatibility**:
   - If using Flow B (embedded modal) on mobile browsers (Safari on iOS, Chrome on Android), always trigger `rzp.open()` directly in response to a user click gesture (e.g., inside an `onClick` event handler) to prevent the mobile browser from blocking the payment popup.

---

## 10. Summary Checklist for Frontend Developers

- [ ] Ensure **zero API keys or secrets** are stored in frontend code, `.env.local`, or client bundles.
- [ ] Determine whether **Hosted Checkout (Flow A)** or **Custom Embedded Modal (Flow B)** best fits the application UX.
- [ ] Implement `callback_url` handling and present a clean confirmation screen to the user.
- [ ] Account for HTTP `429` rate limiting and display clear countdown/retry messages.
- [ ] Test with Sandbox cards and UPI IDs on `http://localhost:5055` before production deployment.
