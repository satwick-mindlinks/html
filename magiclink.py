
@app.route('/verify-magic-link/<token>', methods=['POST'])
def verify_magic_link(token):
    try:
        # Find user by magic link token
        user = User.query.filter_by(magic_link=token).first()
        if not user:
            return jsonify({'message': 'Invalid magic link'}), 401

        current_time = datetime.now(timezone.utc)

        # Check link status and expiration
        if user.magic_link_status != 'active':
            return jsonify({'message': 'Magic link is no longer active'}), 401

        if current_time > user.magic_link_expiry_date:
            user.magic_link_status = 'expired'
            db.session.commit()
            return jsonify({'message': 'Magic link has expired'}), 401

        # Get customer record if user is a customer
        customer = None
        if user.user_role == 'customer':
            customer = Customer.query.filter_by(user_id=user.user_id).first()
            if customer:
                customer_taxfinancial = CustomerTaxFinancial.query.filter_by(customer_id=customer.id).first()
                if customer_taxfinancial and customer_taxfinancial.filing_type == 'Married filing jointly':
                    customer_jointmembers = CustomerJointMember.query.filter_by(customer_id=customer.id).first()
                    if customer_jointmembers:
                        spouse_name = customer_jointmembers.member_name
                    else:
                        spouse_name = None
                else:
                    spouse_name = None

        # Mark link as used and update last login
        user.magic_link_status = 'used'
        user.last_login = current_time

        # Generate auth token - exactly as in login
        auth_token = jwt.encode({
            'user_id': user.user_id,
            'user_role': user.user_role,
            'exp': datetime.now(timezone.utc) + timedelta(hours=24)
        }, app.config['SECRET_KEY'], algorithm="HS256")

        db.session.commit()

        # Match login response structure exactly
        response_data = {
            'token': auth_token,
            'user_id': user.user_id,
            'username': user.username,
            'email': user.email,
            'user_role': user.user_role
        }

        # Add customer data if customer - matching login structure
        if user.user_role == 'customer' and customer and customer_taxfinancial:
            response_data.update({
                'customer': {
                    'customer_id': customer.id,
                    'filing_type': customer_taxfinancial.filing_type,
                    'spouse_name': spouse_name
                }
            })

        return jsonify(response_data), 200

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error verifying magic link: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/generate-magic-link', methods=['POST'])
def generate_magic_link():
    try:
        data = request.get_json()
        email = data.get('email')
        first_name = data.get('first_name')
        last_name = data.get('last_name')

        if not all([email, first_name, last_name]):
            return jsonify({"message": "Email, first name, and last name are required"}), 400

        # Find user and validate first name
        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({"message": "Invalid email"}), 401

        # Get customer record if user is a customer
        customer = None
        if user.user_role == 'customer':
            customer = Customer.query.filter_by(user_id=user.user_id).first()

        # Case-insensitive name comparison
        if (user.first_name.lower() != first_name.lower() or
                user.last_name.lower() != last_name.lower()):
            return jsonify({"message": "Invalid user details"}), 401

        # Generate magic link token
        magic_link_token = str(uuid.uuid4())

        # Use expiry time from config
        expiry_minutes = config['SETTINGS']['MAGIC_LINK']['EXPIRY_MINUTES']
        expiry_time = datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)

        # Update user record with magic link info
        user.magic_link = magic_link_token
        user.magic_link_status = 'active'
        user.magic_link_created_date = datetime.now(timezone.utc)
        user.magic_link_expiry_date = expiry_time

        # Generate magic link URL
        magic_link_url = f"{app.config['FRONTEND_URL']}/magic-link/{magic_link_token}"

        # Send email and create notification
        try:
            templates = load_email_templates()
            template = templates['MAGIC_LINK']['SENT']

            html_content = template['HTML'].format(
                user_name=user.first_name,
                magic_link=magic_link_url,
                expiry_minutes=expiry_minutes
            )

            msg = Message(
                subject=template['SUBJECT'],
                recipients=[user.email],
                html=html_content,
                sender=app.config['MAIL_DEFAULT_SENDER']
            )
            mail.send(msg)

            # Create notification record
            notification = CustomerNotifications(
                customer_id=customer.id if customer else None,
                subject="Magic Link Generated",
                message=f"Magic link generated for {user.email}",
                priority="HIGH",
                status="SENT",
                related_entity_type="MAGIC_LINK",
                related_entity_id=user.user_id,
                created_by=user.user_id,
                created_date=datetime.now(timezone.utc),
                last_modified_by=user.user_id,
                last_modified_date=datetime.now(timezone.utc)
            )
            db.session.add(notification)
            db.session.flush()

            # Create delivery record
            delivery = NotificationDeliveries(
                notification_id=notification.id,
                channel='EMAIL',
                status='SENT',
                sent_date=datetime.now(timezone.utc),
                created_by=user.user_id,
                created_date=datetime.now(timezone.utc),
                last_modified_by=user.user_id,
                last_modified_date=datetime.now(timezone.utc)
            )
            db.session.add(delivery)

        except Exception as e:
            app.logger.error(f"Error sending magic link email or recording notification: {str(e)}")
            # Continue even if email fails

        db.session.commit()

        return jsonify({
            "message": "Magic link sent successfully",
            "expires_in": f"{expiry_minutes} minutes"
        }), 200

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error generating magic link: {str(e)}")
        return jsonify({"message": "An error occurred"}), 500
