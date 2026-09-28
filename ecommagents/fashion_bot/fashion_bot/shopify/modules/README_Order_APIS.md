# Shopify Order Management - Modular APIs (Updated)

## 📄 **What Each Main Python File Does (Simple Explanation)**

### 1. `interactive_bot.py`
- **What:** The main interactive command-line tool for managing Shopify orders.
- **How:** Guides the user step-by-step to create, update, or cancel orders, with prompts for products, address, notes, etc.
- **What's Happening:**
  - Lets you add multiple products to a cart, select variants/sizes, and review before checkout.
  - Handles address entry with pincode-based city/state autofill.
  - Allows updating order address, name, phone, or **changing a product/variant in an existing order** (via cancel-and-recreate flow).
  - Accepts any cancellation reason, adding non-standard ones as notes.
  - Provides 'back' and error handling at every step for a robust user experience.

### 2. `order_creation_api.py`
- **What:** The backend API for creating Shopify orders.
- **How:** Validates all order details, assembles the order data, and sends it to Shopify.
- **What's Happening:**
  - Checks email, phone, address, and product info for validity.
  - Supports single-product and multi-product orders (used by the CLI cart).
  - Handles errors and returns clear success/failure info.

### 3. `order_cancellation_api.py`
- **What:** The backend API for cancelling Shopify orders.
- **How:** Cancels orders on Shopify, and on Shiprocket if the order is fulfilled.
- **What's Happening:**
  - Accepts any cancellation reason; if not standard, adds it as a note before cancelling.
  - Handles both unfulfilled and fulfilled (with Shiprocket) orders.
  - **For fulfilled orders, cancellation is always performed in Shiprocket first, then in Shopify. This is a deliberate flow to ensure proper logistics handling, not a limitation.**
  - Returns detailed results and error info.

### 4. `order_updation_api.py`
- **What:** The backend API for updating Shopify orders.
- **How:** Updates order address, name, phone, or note. For product/variant change, uses a cancel-and-recreate workflow.
- **What's Happening:**
  - Validates and updates address (with pincode/city/state logic), or adds a note.
  - **For product/variant change:**
    - User selects which item to change in a multi-item order.
    - Only the selected item is updated; all others remain unchanged.
    - The original order is cancelled (Shiprocket first if fulfilled, then Shopify), and a new order is created with the updated item.
  - Handles fulfillment-aware logic for address/note updates and product/variant changes.

---

This directory contains modular APIs and an advanced interactive CLI for comprehensive Shopify order management, supporting robust workflows for order creation, cancellation, updating, and product changes.

## 🚀 **Major Features & Updates**

### 1. **Cart / Multi-Product Support**
- The interactive CLI supports adding multiple products (with variants and quantities) to a cart before checkout.
- Cart review, editing (removal), and summary are available before order creation.

### 2. **Product/Variant Selection by Name or Number**
- Products and variants can be selected by typing their number **or** by entering a (partial) name.
- Case-insensitive substring and fuzzy matching are used for robust selection (e.g., typing "shacket" matches "Evolve: The Cosmic Shacket").
- Live product search is performed via Shopify APIs (no local cache).

### 3. **Pincode-Based City/State Autofill**
- During both order creation and address update, entering a pincode will auto-fill city and state using an online lookup.
- User can confirm or override the city/state, or enter them manually if lookup fails.

### 4. **Dummy Email Generation**
- If the customer does not provide an email, a dummy email is generated using their phone number (e.g., `dummy9876543210@bot.in`).

### 5. **Flexible Cancellation Reasons**
- Any cancellation reason is accepted from the user.
- If the reason is not a standard Shopify reason, it is added as a note to the order before cancellation, and "other" is used as the API reason.

### 6. **Full Update Order Workflow**
- After entering the order ID, the user can choose to update the address, add a note, or change the product/variant.
- **Product/Variant Change:**
  - User is shown all items in the order and can select which one to change.
  - User chooses to change the product or just the variant (size) for that item.
  - Only the selected item is updated in the new order; all other items remain unchanged.
  - The new order is summarized for confirmation before creation.
  - The original order is cancelled (Shiprocket first if fulfilled, then Shopify).
- **Address/Note:**
  - Uses the same pincode/city/state logic as order creation.
  - Fulfillment-aware logic for address/note updates.

### 7. **Back/Cancel Options at Every Step**
- At every input step (product, variant, quantity, address, etc.), the user can type 'back' to return to the previous step or cancel the operation.

### 8. **Consistent, User-Friendly Experience**
- All flows (creation, update, cancellation) are robust, interactive, and provide clear feedback and options to the user.

---

## API Structure

### 1. Order Creation API (`order_creation_api.py`)
- Modular, dataclass-based, with full validation and error handling.
- Supports multi-product orders via the CLI.

### 2. Order Cancellation API (`order_cancellation_api.py`)
- Accepts any cancellation reason, with non-standard reasons added as notes.
- Handles Shiprocket cancellation for fulfilled orders (always Shiprocket first, then Shopify).

### 3. Order Updation API (`order_updation_api.py`)
- Supports updating address (with pincode lookup), note, or product/variant (via cancel-and-recreate flow).
- Fulfillment-aware logic for product/variant changes.

---

## Example CLI Flows

### **Order Creation**
- Add multiple products/variants to cart.
- Review/edit cart before checkout.
- Enter address with pincode-based city/state autofill.
- Dummy email generated if not provided.

### **Order Update**
- Choose to update address, note, or product/variant.
- For product/variant change, select which item to update, and only that item is changed in the new order.
- The original order is cancelled (Shiprocket first if fulfilled, then Shopify), and a new order is created with the updated item.
- **For fulfilled orders, address updates are always performed in Shiprocket first, then in Shopify.**

### **Order Cancellation**
- Any reason accepted; non-standard reasons are added as notes.
- Handles Shiprocket cancellation for fulfilled orders (always Shiprocket first, then Shopify).

---

## **Best Practices**
- Always validate inputs and handle errors gracefully.
- Use 'back' at any step to correct mistakes or cancel.
- Review cart and order summaries before finalizing actions.

---

## **Summary**

The modular APIs and interactive CLI now provide a complete, modern, and user-friendly solution for Shopify order management, supporting:
- Multi-product carts
- Robust product/variant selection and update (via cancel-and-recreate)
- Smart address entry and update
- Flexible cancellation and update workflows
- Full error handling and user guidance at every step

> **Note:**
> - Product or variant changes are now supported via a cancel-and-recreate workflow. Only the selected item is updated; all others remain unchanged.
> - For fulfilled orders, all address updates and cancellations are performed in Shiprocket first, then in Shopify. This is a deliberate flow, not a limitation.

For more details, see the code and inline documentation in each module. 