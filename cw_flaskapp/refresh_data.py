from flask_mail import Message
from datetime import datetime, date

from app import db, mail, app
from app.models import Item, ParentItem, Image, Offer, LastRefreshed
from amazon_api import get_parent_ASIN, get_item_attributes, get_amazon_api, get_images, get_item_variations_from_parent, get_offers
from wishlist import get_items_from_wishlist, get_items_from_local_file
from config import get_logger

from sqlalchemy import or_

import os
import logging

logger = get_logger('refresh_data')

WISHLIST_ID = '1ZF0FXNHUY7IG'
MAILTO = 'pete@petecartwright.com'

DEBUG = True

def get_buybox_price(item):
    ''' take an Item object, return the buybox price
        Returns None if not
    '''

    buybox_price = None

    for offer in item.offers.all():
        if offer.offer_source == 'Buybox':
            buybox_price = offer.offer_price_amount

    return buybox_price


def find_best_offer_per_wishlist_item():
    ''' look at all of the items and offers in the wishlist, then flag one offer per item as the best
    '''
    logger.info('Getting the best offers for each item on the wishlist')
    all_wishlist_items = Item.query.filter(Item.is_on_wishlist == True) \
                                   .filter(Item.name != None) \
                                   .all()

    for item in all_wishlist_items:
        # get the list price for the variant we had in the wishlist
        # get the buybox for this item
        best_offer_price = 999999999      # assuming all of our prices will be lower than a billion dollars
        best_offer = None
        buybox_price = get_buybox_price(item)

        # if we have a parent for that item
        # get all variants, including that item
        if item.parent_item:
            all_items_under_parent = item.parent_item.items.all()

            for x in all_items_under_parent:
                for o in x.offers.all():
                    # reset the best offer tracking from last item
                    o.best_offer = False
                    if o.offer_price_amount < best_offer_price:
                        best_offer = o
                        best_offer_price = o.offer_price_amount

            if best_offer:
                # we need a price to compare it to.
                # if we have the list price, use that. If not, use the buybox
                # if not, use 0
                comparison_price = 0
                if item.list_price_amount:
                    comparison_price = item.list_price_amount
                elif buybox_price:
                    comparison_price = buybox_price

                # mark the best offer
                logger.info('   Best Offer for {0} is {1}'.format(item.name, best_offer))
                best_offer.best_offer = True
                best_offer.savings_vs_list = comparison_price - best_offer_price
                best_offer.wishlist_item_id = item.id
                db.session.add(best_offer)
                db.session.commit()
            else:
                logger.info('No best offer for {0}'.format(item.name))


def get_images_for_best_offer_items(amazon_api):

    best_offers = Offer.query.filter( Offer.best_offer == True ).all()
    
    number_of_offers = len(best_offers)
    index = 1

    for offer in best_offers:
        item = offer.item 
        percent_complete = round((float(index) / float(number_of_offers) * 100 ), 2) 
        logger.info('   Getting image for {0} ({1} of {2}, {3}%'.format(item.name, index, number_of_offers, percent_complete))
        
        # only get images if we don't have any
        if len(item.images.all()):
            logger.info('   Already have it, moving on')
        else:
            ASIN = item.ASIN
            # get the main image for the item
            item_image = get_images(ASIN=ASIN, amazon_api=amazon_api)
            image_sizes = get_image_sizes(item_image)
            new_item_image = Image(smallURL=str(image_sizes['smallURL']),
                                smallHeight=int(image_sizes['smallHeight'] or 0),
                                smallWidth=int(image_sizes['smallWidth'] or 0),
                                mediumURL=str(image_sizes['mediumURL']),
                                mediumHeight=int(image_sizes['mediumHeight'] or 0),
                                mediumWidth=int(image_sizes['mediumWidth'] or 0),
                                largeURL=str(image_sizes['largeURL']),
                                largeHeight=int(image_sizes['largeHeight'] or 0),
                                largeWidth=int(image_sizes['largeWidth'] or 0),
                                item_id=item.id)
            db.session.add(new_item_image)
            db.session.commit()

        index += 1


def find_cheapest_overall_and_vs_list():

    # remove any offers that are currently cheapest
    current_cheapest = Offer.query.filter( Offer.cheapest_overall == True).first()
    current_cheapest.cheapest_overall = False
    db.session.add(current_cheapest)

    current_cheapest_vs = Offer.query.filter( Offer.cheapest_vs_list == True).first()
    current_cheapest_vs.cheapest_vs_list = False
    db.session.add(current_cheapest_vs)
    db.session.commit()

    # now identify the NEW cheapest overall and cheapest vs list
    all_offers = Offer.query.filter(Offer.best_offer == True).all()

    cheapest_overall_offer = sorted(all_offers, key=lambda k: k.offer_price_amount)[0]
    cheapest_overall_offer.cheapest_overall = True

    cheapest_offer_vs_list = sorted(all_offers, key=lambda k: k.savings_vs_list, reverse=True)[0]
    cheapest_offer_vs_list.cheapest_vs_list = True

    db.session.add(cheapest_overall_offer)
    db.session.add(cheapest_offer_vs_list)
    db.session.commit()


def add_wishlist_items_to_db(wishlist_items):
    for i in wishlist_items:
        logger.info('Checking to see if {0} is in our db already'.format(i['ASIN']))
        # check to see if we already have it, if not, add it to the database
        item_to_add = Item.query.filter_by(ASIN=i['ASIN']).first()

        if item_to_add is None:
            logger.info('   Don''t have it, adding now')
            item_to_add = Item(ASIN=i['ASIN'])
            item_to_add.is_on_wishlist = True
            db.session.add(item_to_add)
            db.session.commit()
        else:
            logger.info('   Yep, we had it')


def get_all_parents(all_items):

    for i in all_items:
        logger.info('getting parent for {0}'.format(i.ASIN))
        # if we don't have a parent, get one
        if i.parent_item:
            logger.info('already have parent for {0}'.format(i.ASIN))
        else:    
            item_parent_ASIN = get_parent_ASIN(ASIN=i.ASIN, amazon_api=amazon_api)
            logger.info('   got parent')
            # if this parent doesn't exist, create it
            parent = ParentItem.query.filter_by(parent_ASIN=item_parent_ASIN).first()
            if parent is None:
                logger.info("parent doesn't exist, creating")
                parent = ParentItem(parent_ASIN=item_parent_ASIN)
                db.session.add(parent)
                db.session.commit()
            # add the parent to the item
            i.parent_item = parent
            db.session.add(i)
            db.session.commit()


def get_all_variations_under_parents(parent_items, amazon_api):

    total_parents_to_check = len(parent_items)
    index = 1

    for p in parent_items:
        percent_done = str(round(100 * (float(index) / float(total_parents_to_check)), 2))
        logger.info('getting variations for {0} ({1} / {2}, {3}%)'.format(p.parent_ASIN, index, total_parents_to_check, percent_done))
        # get a list of all ASINS under that parent
        logger.info('getting variations for {0}'.format(p.parent_ASIN))
        variations = get_variations(parent_ASIN=p.parent_ASIN, amazon_api=amazon_api)
        logger.info('Found {0} variations for {1}'.format(len(variations), p.parent_ASIN))
        for v in variations:
            logger.info('   Checking for existence of variation {0}'.format(v))
            var = Item.query.filter_by(ASIN=v).all()
            if len(var) == 0:
                logger.info("       Don't have this one, adding.")
                # if we don't have these variations already, add them to the database
                # with the correct parent
                new_variation = Item(ASIN=v, parent_item=p)
                db.session.add(new_variation)
                db.session.commit()
            else:
                logger.info("       Have it.")
        index +=1


def get_image_sizes(item_image):
    """ Amazon API isn't reliable about sending the same fields back every ThumbnailImage
        This takes an item_image dict and returns what it can get as another dict.
    """

    smallImage = item_image.get('SmallImage')
    mediumImage = item_image.get('MediumImage')
    largeImage = item_image.get('LargeImage')

    if smallImage:
        smallURL = smallImage.get('URL')
        smallHeight = smallImage.get('Height')
        smallWidth = smallImage.get('Width')
    else:
        smallURL = ''
        smallHeight = ''
        smallWidth = ''

    if mediumImage:
        mediumURL = mediumImage.get('URL')
        mediumHeight = mediumImage.get('Height')
        mediumWidth = mediumImage.get('Width')
    else:
        mediumURL = ''
        mediumHeight = ''
        mediumWidth = ''

    if largeImage:
        largeURL = largeImage.get('URL')
        largeHeight = largeImage.get('Height')
        largeWidth = largeImage.get('Width')
    else:
        largeURL = ''
        largeHeight = ''
        largeWidth = ''

    image_sizes = {'smallURL': smallURL,
                   'smallHeight': smallHeight,
                   'smallWidth': smallWidth,
                   'mediumURL': mediumURL,
                   'mediumHeight': mediumHeight,
                   'mediumWidth': mediumWidth,
                   'largeURL': largeURL,
                   'largeHeight': largeHeight,
                   'largeWidth': largeWidth}

    return image_sizes


def get_variations(parent_ASIN, amazon_api=None):
    ''' take an ASIN and amazon API object, get all ASINs for all variations for that items
        if nothing is found, return an array with just that ASIN
    '''
    if amazon_api is None:
        amazon_api = get_amazon_api()

    if parent_ASIN:
        variations = get_item_variations_from_parent(parentASIN=parent_ASIN, amazon_api=amazon_api)
        return variations
    else:
        return [parent_ASIN]


def refresh_item_data(item, amazon_api=None):
    ''' Take an Item object and update the data in the database
        Returns True if everything goes well
        Returns False if this isn't an item we can get through the API

        TODO - literally any error handling
    '''

    if amazon_api is None:
        amazon_api = get_amazon_api()

    ASIN = item.ASIN
    
    logger.info('refreshing data for item {0}'.format(ASIN))

    # get other item attribs, only if we don't already have them
    logger.info('   getting attributes')
    if item.name is None:
        item_attributes = get_item_attributes(ASIN, amazon_api=amazon_api)

        if item_attributes == {}:
          return False

        # using .get() here because it will default to None is the key is
        # not in the dict, and the API is not reliable about sending everything back
        item.list_price_amount = item_attributes.get('listPriceAmount')
        item.list_price_formatted = item_attributes.get('listPriceFormatted')
        item.product_group = item_attributes.get('product_group')
        item.name = item_attributes.get('title')
        item.URL = item_attributes.get('URL')
        item.date_last_checked = datetime.date(datetime.today())
        item.is_cookbook = item_attributes.get('is_cookbook') 

        db.session.add(item)
        db.session.commit()
        logger.info('   got attributes')

    return True


def update_last_refreshed():
    ''' Remove the last_refreshed date and replace it with now
    '''
    deleted_last_refreshed = LastRefreshed.query.delete()
    last = LastRefreshed()
    last.last_refreshed = datetime.now()
    db.session.add(last)
    db.session.commit()
    return deleted_last_refreshed


def send_completion_message():
    msg = Message("WSIBPT has refreshed", sender="pete.cartwright@gmail.com", recipients=[MAILTO])
    with app.app_context():
        mail.send(msg)



def find_cheapest_overall_and_vs_list():

    all_best_offers = Offer.query.filter(Offer.best_offer == True).all()

    cheapest_vs_list = sorted(all_best_offers, key=lambda k: k.savings_vs_list, reverse=True)[0]
    cheapest_overall = sorted(all_best_offers, key=lambda k: k.offer_price_amount, reverse=True)[0]

    cheapest_vs_list.cheapest_vs_list = True
    cheapest_overall.cheapest_overall = True
    db.session.add(cheapest_overall)
    db.session.add(cheapest_vs_list)
    db.session.commit()
    

def main():

    amazon_api = get_amazon_api()

    todays_date = date.today()

    if DEBUG:
        logger.info('loading items from local file')
        wishlist_items = get_items_from_local_file()
    else:
        # scan the wishlist on Amazon's site
        logger.info('loading items from amazon')
        wishlist_items = get_items_from_wishlist(WISHLIST_ID)

    add_wishlist_items_to_db(wishlist_items=wishlist_items)

    # now that all of the base items are in the wishlist, get all of the parent items

    all_items = Item.query.filter(or_(Item.date_last_checked == None, Item.date_last_checked != todays_date)).all()

    get_all_parents(all_items=all_items)
            
    # from that list of parents, get all variations
    all_parents = ParentItem.query.filter(or_(Item.date_last_checked == None, Item.date_last_checked != todays_date)).all()
    
    get_all_variations_under_parents(parent_items=all_parents, amazon_api=amazon_api)

    ## Next step is to get the item data for everything in the database

    # get attributes (name, price, URL, etc) for all items
    # all all offers for each item
    all_items_with_variations = Item.query.filter(or_(Item.date_last_checked == None, Item.date_last_checked != todays_date)).all()

    number_of_items_to_check = len(all_items_with_variations)
    index = 1

    for i in all_items_with_variations:
        percent_done = str(round(100 * (float(index) / float(number_of_items_to_check)), 2))
        logger.info('in the item refresh for {0} ({1} / {2}, {3}%)'.format(i.ASIN, index, number_of_items_to_check, percent_done))
        refresh_item_data(item=i, amazon_api=amazon_api)
        # cant' get info on some - looks like maybe weapons?
        if i.name is not None:
            # get all of the available offers
            # first remove existing offers from database
            item_offers = i.offers.all()
            for x in item_offers:
                logger.info('   removing old offers for {0}'.format(i.ASIN))
                db.session.delete(x)
                db.session.commit()
            # can't get offers for Kindle Books
            if i.product_group == 'eBooks':
                logger.info("   can't get offers for Kindle books")
            else:
                logger.info('   getting offers for {0}'.format(i.ASIN))
                offers = get_offers(item=i, amazon_api=amazon_api)
                for o in offers:
                    new_offer = Offer()
                    new_offer.condition = str(o['condition'])
                    new_offer.offer_source = str(o['offer_source'])
                    new_offer.offer_price_amount = int(o['offer_price_amount'])
                    new_offer.offer_price_formatted = str(o['offer_price_formatted'])
                    new_offer.prime_eligible = o['prime_eligible']
                    new_offer.availability = str(o['availability'])
                    new_offer.item_id = o['item_id']
                    new_offer.item = i
                    db.session.add(new_offer)
                    db.session.commit()
        index += 1

    # now let's see what the best deals are!
    find_best_offer_per_wishlist_item()

    get_images_for_best_offer_items(amazon_api)

    find_cheapest_overall_and_vs_list()

    update_last_refreshed()

    logger.info('Finished run at {0}'.format(datetime.now().strftime('%H:%M %Y-%m-%d')))

    send_completion_message()

if __name__ == '__main__':

    main()
